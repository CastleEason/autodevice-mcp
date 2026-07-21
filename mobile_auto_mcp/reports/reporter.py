"""Archive report writer for completed sessions."""

from __future__ import annotations

import html
import json
import shutil
from collections import OrderedDict
from pathlib import Path
from typing import Any

from mobile_auto_mcp.execution.events import ExecutionEventStore
from mobile_auto_mcp.state.private_files import atomic_write_private_text, ensure_private_directory, resolve_within
from mobile_auto_mcp.state.storage import LocalStore, now


def export_archive_report(store: LocalStore, session_id: str, report_dir: str | Path | None = None) -> dict[str, Any]:
    """导出包含执行、代理和完整改写响应证据的可审计报告包。"""
    requested = Path(report_dir).expanduser() if report_dir else store.home / "reports" / session_id
    if not requested.is_absolute():
        requested = store.home / requested
    out = resolve_within(store.home, requested)
    ensure_private_directory(out)
    runs = store.list_runs(session_id)
    session = store.get_session(session_id)
    summary = store.session_summary(session_id)
    copied = _copy_screenshots(out, runs, allowed_root=store.home)
    events = store.proxy_state.read_events(session_id=session_id, limit=10000)
    execution_events = ExecutionEventStore(store.home).read(session_id=session_id, limit=10000)
    modified_responses = store.proxy_state.read_modified_responses(session_id=session_id)
    hits = store.proxy_state.list_hits(session_id=session_id, limit=10000)
    integrity = _report_integrity(runs, execution_events, modified_responses, hits=hits, session=session)
    manual_proxy_action = _manual_proxy_action(session)
    summary = {**summary, "integrity": integrity}
    atomic_write_private_text(out / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
    atomic_write_private_text(out / "runs.json", json.dumps(runs, ensure_ascii=False, indent=2))
    atomic_write_private_text(out / "modified_responses.json", json.dumps(modified_responses, ensure_ascii=False, indent=2))
    atomic_write_private_text(
        out / "mitmproxy_events.jsonl",
        "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in events),
    )
    atomic_write_private_text(
        out / "execution_events.jsonl",
        "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in execution_events),
    )
    atomic_write_private_text(
        out / "trace.json",
        json.dumps(
            {
                "events": events,
                "execution_events": execution_events,
                "hits": hits,
                "modified_responses": modified_responses,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    markdown = _markdown_report(session_id, summary, runs, integrity, manual_proxy_action)
    atomic_write_private_text(out / "report.md", markdown)
    atomic_write_private_text(
        out / "index.html",
        _html_report(session_id, summary, runs, modified_responses, integrity, manual_proxy_action),
    )
    manifest = {
        "session_id": session_id,
        "generated_at": now(),
        "files": [
            "summary.json",
            "runs.json",
            "modified_responses.json",
            "trace.json",
            "mitmproxy_events.jsonl",
            "execution_events.jsonl",
            "report.md",
            "index.html",
        ],
        "screenshots": copied,
        "integrity": integrity,
        "manual_proxy_action": manual_proxy_action,
    }
    atomic_write_private_text(out / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    hub = _write_report_hub(out.parent)
    return {"report_dir": str(out), "markdown": str(out / "report.md"), "html": str(out / "index.html"), "hub": str(hub), "summary": summary}


def _copy_screenshots(out: Path, runs: list[dict[str, Any]], *, allowed_root: Path) -> list[str]:
    """Copy only regular screenshots contained by the caller-authorized data root."""
    shots_dir = out / "screenshots"
    ensure_private_directory(shots_dir)
    trusted_root = allowed_root.expanduser().resolve()
    copied: list[str] = []
    for run in runs:
        for item in run.get("screenshots") or []:
            src = Path(item).expanduser()
            try:
                resolved = src.resolve(strict=True)
                resolved.relative_to(trusted_root)
            except (FileNotFoundError, OSError, ValueError):
                continue
            if not resolved.is_file():
                continue
            dst = shots_dir / resolved.name
            if resolved != dst.resolve():
                shutil.copy2(resolved, dst)
            copied.append(str(dst))
    return copied


def _markdown_report(
    session_id: str,
    summary: dict[str, Any],
    runs: list[dict[str, Any]],
    integrity: dict[str, Any],
    manual_proxy_action: dict[str, Any],
) -> str:
    """生成包含证据完整性结论的 Markdown 执行摘要。"""
    lines = [
        "# mobile_auto_mcp 执行报告",
        "",
        f"- Session: `{session_id}`",
        f"- 记录数: {summary.get('records', 0)}",
        f"- 待审查: {summary.get('pending_review', 0)}",
        f"- 执行无效: {summary.get('invalid_execution', 0)}",
        f"- 截图数: {summary.get('screenshots', 0)}",
        f"- 证据完整性: {integrity.get('status', 'failed')}",
        *(
            [f"- ⚠️ 代理提醒: {manual_proxy_action.get('reminder', '')}"]
            if manual_proxy_action.get("required")
            else []
        ),
        "",
        "| Lane | 规则 | API | 状态 | 请求数 | 截图 | 备注 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for run in runs:
        screenshot = run.get("screenshot") or ""
        lane_id = (run.get("traceability") or {}).get("lane_id", "")
        request_count = (run.get("execution_gate") or {}).get("request_count", "")
        lines.append(
            f"| {lane_id} | {run.get('case_name','')} | `{_request_contract_label(run)}` | {run.get('status','')} | {request_count} | {Path(screenshot).name if screenshot else ''} | {run.get('review_note','')} |"
        )
    return "\n".join(lines) + "\n"


def _html_report(
    session_id: str,
    summary: dict[str, Any],
    runs: list[dict[str, Any]],
    modified_responses: list[dict[str, Any]],
    integrity: dict[str, Any],
    manual_proxy_action: dict[str, Any],
) -> str:
    """生成按用例聚合的三端 H5 报告，并提供截图大图查看能力。"""
    case_groups = _group_case_runs(runs)
    rows = "\n".join(
        _render_case_row(rule_id, case_runs)
        + _render_modified_response_row(rule_id, _modified_responses_for_case(rule_id, modified_responses))
        for rule_id, case_runs in case_groups.items()
    )
    case_count = len(case_groups)
    passed_cases = sum(1 for case_runs in case_groups.values() if _case_status(case_runs) == "passed")
    failed_cases = sum(1 for case_runs in case_groups.values() if _case_status(case_runs) == "failed")
    if integrity.get("status") != "passed":
        overall = "证据不完整"
    else:
        overall = "通过" if case_count and passed_cases == case_count else "存在待处理项"
    integrity_note = "；".join(str(issue.get("message") or issue.get("code")) for issue in integrity.get("issues") or [])
    proxy_reminder = (
        f'<section class="proxy-reminder"><strong>代理仍在运行</strong><span>{html.escape(str(manual_proxy_action.get("reminder") or ""))}</span></section>'
        if manual_proxy_action.get("required")
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>mobile_auto_mcp 三端验收报告</title>
  <style>
    :root {{ color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif; --bg:#f4f6f8; --panel:#fff; --line:#d9e0e7; --text:#18212b; --muted:#667085; --pass:#087443; --fail:#b42318; --accent:#175cd3; }}
    * {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--text); font-size:14px; }}
    main {{ max-width:1920px; margin:0 auto; padding:24px; }}
    header {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-start; margin-bottom:16px; }}
    h1 {{ margin:0 0 8px; font-size:26px; letter-spacing:0; }} h2 {{ margin:0; font-size:16px; }}
    .muted {{ color:var(--muted); }} .summary {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; }}
    .metric {{ min-width:110px; padding:9px 12px; background:var(--panel); border:1px solid var(--line); border-radius:6px; }}
    .metric strong {{ display:block; margin-top:2px; font-size:18px; }}
    .overall {{ min-width:210px; padding:14px 16px; background:var(--panel); border:1px solid var(--line); border-radius:6px; }}
    .overall strong {{ display:block; margin-top:6px; color:{'var(--pass)' if overall == '通过' else 'var(--fail)'}; font-size:24px; }}
    .proxy-reminder {{ display:flex; gap:10px; align-items:center; margin:0 0 14px; padding:11px 13px; color:#7a2e0e; background:#fff7ed; border:1px solid #fdba74; border-radius:6px; }}
    .table-wrap {{ overflow:auto; background:var(--panel); border:1px solid var(--line); border-radius:6px; }}
    table {{ width:100%; min-width:1780px; border-collapse:collapse; table-layout:fixed; }}
    th, td {{ padding:10px; border-right:1px solid var(--line); border-bottom:1px solid var(--line); vertical-align:top; text-align:left; overflow-wrap:anywhere; }}
    th {{ position:sticky; top:0; z-index:2; background:#f8fafc; color:#344054; font-size:12px; }}
    tr:last-child td {{ border-bottom:0; }} th:last-child, td:last-child {{ border-right:0; }}
    .case-col {{ width:190px; }} .api-col {{ width:270px; }} .expect-col {{ width:220px; }} .platform-col {{ width:235px; }} .conclusion-col {{ width:280px; }} .status-col {{ width:90px; }}
    .case-name {{ font-weight:750; margin-bottom:6px; }} code {{ color:#344054; background:#f2f4f7; padding:2px 4px; border-radius:3px; }}
    .evidence-image {{ display:block; width:100%; height:238px; object-fit:contain; background:#eef2f6; border:1px solid var(--line); border-radius:4px; cursor:zoom-in; }}
    .platform-name {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:7px; font-weight:700; }}
    .note {{ margin-top:7px; color:#475467; font-size:12px; line-height:1.5; }}
    .change {{ margin:6px 0; padding:7px; background:#f8fafc; border-left:3px solid var(--accent); font-size:12px; }}
    .modified-response-row td {{ padding:0; background:#fbfcfe; }} .modified-response-panel {{ padding:12px; border-bottom:2px solid var(--line); }}
    .modified-response-panel h3 {{ margin:0 0 8px; font-size:14px; }} .modified-response-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; }}
    .modified-response-lane {{ min-width:0; }} .modified-response-lane h4 {{ margin:0 0 6px; color:#344054; }}
    .modified-response-lane details {{ margin-bottom:6px; border:1px solid var(--line); border-radius:4px; background:#fff; }}
    .modified-response-lane summary {{ padding:7px; cursor:pointer; font-weight:650; }} .modified-response-lane pre {{ margin:0; padding:8px; max-height:360px; overflow:auto; border-top:1px solid var(--line); background:#101828; color:#f2f4f7; font-size:11px; white-space:pre-wrap; }}
    .visual-metrics {{ margin-top:8px; display:grid; gap:5px; }} .visual-metric {{ padding:6px 7px; background:#f8fafc; border:1px solid var(--line); border-radius:4px; font-size:11px; }}
    .value {{ margin-top:4px; white-space:pre-wrap; color:#344054; }}
    .badge {{ display:inline-flex; padding:3px 7px; border-radius:999px; font-size:12px; font-weight:700; }}
    .badge.passed {{ color:var(--pass); background:#ecfdf3; }} .badge.failed {{ color:var(--fail); background:#fef3f2; }} .badge.needs_check {{ color:#9a6700; background:#fffaeb; }}
    dialog {{ width:min(96vw, 1100px); max-height:94vh; padding:0; border:0; border-radius:6px; background:#101828; }} dialog::backdrop {{ background:rgba(0,0,0,.72); }}
    .viewer-bar {{ display:flex; justify-content:space-between; align-items:center; padding:10px 12px; color:#fff; }}
    .viewer-bar button {{ border:0; background:#fff; color:#101828; border-radius:4px; padding:6px 10px; cursor:pointer; }}
    #viewer-image {{ display:block; width:100%; max-height:84vh; object-fit:contain; }}
    @media (max-width:800px) {{ main {{ padding:12px; }} header {{ display:block; }} .overall {{ margin-top:12px; }} .modified-response-grid {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <section>
      <h1>三端字段验收报告</h1>
      <div class="muted">Session: {html.escape(session_id)} · 各端独立执行；同一用例在全部预期端到达终态后异步封账</div>
      <div class="summary">
        <div class="metric"><span class="muted">用例</span><strong>{case_count}</strong></div>
        <div class="metric"><span class="muted">端侧执行</span><strong>{summary.get('records', 0)}</strong></div>
        <div class="metric"><span class="muted">通过用例</span><strong>{passed_cases}</strong></div>
        <div class="metric"><span class="muted">失败用例</span><strong>{failed_cases}</strong></div>
        <div class="metric"><span class="muted">请求改写命中</span><strong>{_applied_gate_count(runs)}/{summary.get('records', 0)}</strong></div>
      </div>
    </section>
    <aside class="overall"><span class="muted">最终结论</span><strong>{overall}</strong><div class="note">判定依据：接口改写门禁、三端截图、逐端视觉复核和证据完整性。</div><div class="note">{html.escape(integrity_note)}</div></aside>
  </header>
  {proxy_reminder}
  <section class="table-wrap">
    <table>
      <thead><tr><th class="case-col">用例</th><th class="api-col">mitmproxy 修改数据</th><th class="expect-col">预期结果</th><th class="platform-col">Android</th><th class="platform-col">iOS</th><th class="platform-col">HarmonyOS</th><th class="conclusion-col">三端差异与具体结论</th><th class="status-col">判定</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </section>
</main>
<dialog id="image-viewer"><div class="viewer-bar"><span id="viewer-title">截图</span><button type="button" id="viewer-close">关闭</button></div><img id="viewer-image" alt="放大的端侧截图"></dialog>
<script>
  const viewer = document.getElementById('image-viewer');
  const viewerImage = document.getElementById('viewer-image');
  const viewerTitle = document.getElementById('viewer-title');
  document.querySelectorAll('.evidence-image').forEach((image) => image.addEventListener('click', () => {{ viewerImage.src = image.src; viewerTitle.textContent = image.alt; viewer.showModal(); }}));
  document.getElementById('viewer-close').addEventListener('click', () => viewer.close());
  viewer.addEventListener('click', (event) => {{ if (event.target === viewer) viewer.close(); }});
</script>
</body>
</html>"""


def _group_case_runs(runs: list[dict[str, Any]]) -> OrderedDict[str, list[dict[str, Any]]]:
    """按规则聚合端侧执行记录，并保持用例首次执行顺序。"""
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for run in sorted(runs, key=lambda item: item.get("created_at", "")):
        grouped.setdefault(str(run.get("rule_id") or run.get("case_name") or run.get("id")), []).append(run)
    return grouped


def _render_case_row(rule_id: str, runs: list[dict[str, Any]]) -> str:
    """把同一用例的接口证据、三端截图和裁决渲染到一行。"""
    first = runs[0]
    by_target = {str(run.get("target") or (run.get("traceability") or {}).get("lane_id")): run for run in runs}
    status = _case_status(runs)
    conclusion = _case_conclusion(runs)
    return (
        f'<tr data-case-row="{html.escape(rule_id)}">'
        f'<td><div class="case-name">{html.escape(str(first.get("case_name") or "未命名用例"))}</div><code>{html.escape(rule_id)}</code></td>'
        f'<td><code>{html.escape(_request_contract_label(first))}</code>{_render_change_evidence(runs)}</td>'
        f'<td>{html.escape(str(first.get("expected") or ""))}</td>'
        f'{_render_platform_cell(by_target.get("android"), "Android")}'
        f'{_render_platform_cell(by_target.get("ios"), "iOS")}'
        f'{_render_platform_cell(by_target.get("harmony"), "HarmonyOS")}'
        f'<td>{html.escape(conclusion)}{_render_visual_metrics(runs)}</td>'
        f'<td><span class="badge {status}">{_status_label(status)}</span></td>'
        '</tr>'
    )


def _modified_responses_for_case(rule_id: str, modified_responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """筛选指定规则的改写响应，并保持代理实际记录顺序。"""
    return [item for item in modified_responses if str(item.get("rule_id") or "") == rule_id]


def _render_modified_response_row(rule_id: str, modified_responses: list[dict[str, Any]]) -> str:
    """在用例主行下按端和命中序号展示每一份完整改写后响应 JSON。"""
    lane_labels = {"android": "Android", "ios": "iOS", "harmony": "HarmonyOS"}
    lanes: list[str] = list(lane_labels)
    lanes.extend(
        lane for lane in dict.fromkeys(str(item.get("lane_id") or "unknown") for item in modified_responses) if lane not in lanes
    )
    rendered_lanes: list[str] = []
    for lane_id in lanes:
        lane_items = [item for item in modified_responses if str(item.get("lane_id") or "unknown") == lane_id]
        details: list[str] = []
        for item in lane_items:
            response_json = html.escape(json.dumps(item.get("modified_response"), ensure_ascii=False, indent=2))
            evidence_id = html.escape(str(item.get("id") or ""), quote=True)
            sequence = int(item.get("sequence") or 0)
            status_code = item.get("status_code")
            status_text = f" · HTTP {status_code}" if status_code is not None else ""
            details.append(
                f'<details data-modified-response="{evidence_id}"><summary>第 {sequence} 次改写{html.escape(status_text)}</summary><pre>{response_json}</pre></details>'
            )
        content = "".join(details) or '<div class="note">无改写后响应证据</div>'
        label = html.escape(lane_labels.get(lane_id, lane_id))
        rendered_lanes.append(f'<section class="modified-response-lane"><h4>{label}</h4>{content}</section>')
    return (
        f'<tr class="modified-response-row" data-response-case="{html.escape(rule_id, quote=True)}"><td colspan="8">'
        f'<section class="modified-response-panel"><h3>改写后响应 JSON（逐端逐次完整保存）</h3>'
        f'<div class="modified-response-grid">{"".join(rendered_lanes)}</div></section></td></tr>'
    )


def _report_integrity(
    runs: list[dict[str, Any]],
    execution_events: list[dict[str, Any]],
    modified_responses: list[dict[str, Any]],
    hits: list[dict[str, Any]] | None = None,
    session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """校验报告所需关键证据，任何缺口都阻止报告给出通过结论。"""
    issues: list[dict[str, str]] = []
    if not runs:
        issues.append({"code": "runs_empty", "message": "报告没有任何规则执行记录"})
    if runs and not execution_events:
        issues.append({"code": "execution_events_empty", "message": "execution_events.jsonl 为空"})
    hit_rows = hits or []
    for run in runs:
        gate = run.get("execution_gate") or {}
        if not gate.get("change_applied"):
            issues.append(
                {
                    "code": "response_change_unproven",
                    "message": f"{run.get('target') or 'unknown'} / {run.get('rule_id') or 'unknown'} 没有响应改写证据",
                }
            )
        traceability = run.get("traceability") or {}
        lane_id = str(traceability.get("lane_id") or run.get("target") or "")
        rule_id = str(run.get("rule_id") or "")
        activation_id = str(traceability.get("activation_id") or "")
        page_anchor = gate.get("page_anchor") or {}
        if not (page_anchor.get("ok") and page_anchor.get("verified") and not page_anchor.get("skipped")):
            issues.append(
                {
                    "code": "page_anchor_unverified",
                    "message": f"{lane_id or 'unknown'} / {rule_id or 'unknown'} 缺少已验证且未跳过的页面锚点",
                }
            )
        if str(run.get("status") or "") not in {"passed", "failed"}:
            issues.append(
                {
                    "code": "final_review_incomplete",
                    "message": f"{lane_id or 'unknown'} / {rule_id or 'unknown'} 尚未完成最终语义或人工复核",
                }
            )
        matching_responses = [
            item
            for item in modified_responses
            if str(item.get("lane_id") or "") == lane_id
            and str(item.get("rule_id") or "") == rule_id
            and (not activation_id or str(item.get("activation_id") or "") == activation_id)
        ]
        if not matching_responses:
            issues.append(
                {
                    "code": "modified_response_missing",
                    "message": f"{lane_id or 'unknown'} / {rule_id or 'unknown'} / {activation_id or 'unknown'} 缺少关联的改写后响应 JSON",
                }
            )
        matching_hits = [
            item
            for item in hit_rows
            if str(item.get("lane_id") or "") == lane_id
            and str(item.get("rule_id") or "") == rule_id
            and (not activation_id or str(item.get("activation_id") or "") == activation_id)
        ]
        if not matching_hits:
            issues.append(
                {
                    "code": "proxy_hit_missing",
                    "message": f"{lane_id or 'unknown'} / {rule_id or 'unknown'} 缺少同 activation 的代理 hit",
                }
            )
    lifecycle = (session or {}).get("proxy_lifecycle") or {}
    if not lifecycle:
        issues.append({"code": "proxy_lifecycle_missing", "message": "报告缺少代理生命周期复核记录"})
    elif not lifecycle.get("verified", False):
        issues.append({"code": "proxy_lifecycle_unverified", "message": "手机代理设置或保留状态未通过复核"})
    return {
        "status": "failed" if issues else "passed",
        "issues": issues,
        "execution_event_count": len(execution_events),
        "modified_response_count": len(modified_responses),
    }


def _manual_proxy_action(session: dict[str, Any]) -> dict[str, Any]:
    """Expose retained proxy details and the user action required after evidence collection is no longer needed."""
    lifecycle = (session or {}).get("proxy_lifecycle") or {}
    required = bool(lifecycle.get("manual_cleanup_required"))
    return {
        "required": required,
        "reminder": str(lifecycle.get("user_reminder") or "") if required else "",
        "proxy_host": str(lifecycle.get("proxy_host") or ""),
        "proxy_port": int(lifecycle.get("proxy_port") or 0),
    }


def _render_platform_cell(run: dict[str, Any] | None, platform: str) -> str:
    """渲染单个平台的截图、请求门禁和视觉复核结论。"""
    safe_platform = html.escape(platform)
    if not run:
        return f'<td><div class="platform-name">{safe_platform}</div><span class="badge needs_check">未执行</span></td>'
    screenshot = str(run.get("screenshot") or "")
    image = ""
    if screenshot:
        src = f"screenshots/{html.escape(Path(screenshot).name)}"
        image = f'<img class="evidence-image" src="{src}" alt="{safe_platform} · {html.escape(str(run.get("case_name") or "用例截图"))}">'
    gate = run.get("execution_gate") or {}
    gate_text = f"请求改写：{'命中' if gate.get('change_applied') else '未命中'} · {gate.get('request_count', 0)} 次"
    final_review = run.get("visual_review") or {}
    precheck = run.get("visual_precheck") or {}
    note = str(run.get("review_note") or final_review.get("conclusion") or "待最终语义复核")
    precheck_note = str(precheck.get("case_conclusion") or "")
    failure = _extract_failure(gate.get("failure") or run.get("failure") or {})
    failure_html = ""
    if failure:
        details = " · ".join(
            item
            for item in (
                str(failure.get("code") or ""),
                str(failure.get("next_action") or ""),
                str(failure.get("remediation") or ""),
            )
            if item
        )
        failure_html = f'<div class="note failure">{html.escape(details)}</div>'
    status = str(run.get("status") or "needs_check")
    safe_status = status if status in {"passed", "failed", "needs_check", "pending_review", "invalid_execution"} else "needs_check"
    precheck_html = f'<div class="note">视觉预检：{html.escape(precheck_note)}</div>' if precheck_note else ""
    return f'<td><div class="platform-name"><span>{safe_platform}</span><span class="badge {safe_status}">{_status_label(safe_status)}</span></div>{image}<div class="note">{html.escape(gate_text)}</div>{precheck_html}<div class="note">最终复核：{html.escape(note)}</div>{failure_html}</td>'


def _extract_failure(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize direct and preflight-wrapped failures for report rendering."""
    if not isinstance(payload, dict):
        return {}
    if payload.get("code"):
        return payload
    failures = payload.get("failures") or []
    return failures[0] if failures and isinstance(failures[0], dict) else {}


def _render_change_evidence(runs: list[dict[str, Any]]) -> str:
    """从首个有效请求证据提取实际修改字段和修改后值。"""
    changes: list[dict[str, Any]] = []
    for run in runs:
        for evidence in run.get("evidence") or []:
            changes = [item for item in (evidence.get("patch_evidence") or []) + (evidence.get("mutation_evidence") or []) if item.get("applied")]
            if changes:
                break
        if changes:
            break
    if not changes:
        return '<div class="change">无可用修改证据</div>'
    rendered = []
    for change in changes:
        field = html.escape(str(change.get("field") or "未知字段"))
        action = html.escape(str(change.get("action") or "修改"))
        value = html.escape(_display_value(change.get("after"), bool(change.get("after_exists", True))))
        rendered.append(f'<div class="change"><strong>{field}</strong> · {action}<div class="value">修改后：{value}</div></div>')
    return "".join(rendered)


def _display_value(value: Any, exists: bool) -> str:
    """把接口值转换为适合报告阅读的稳定文本。"""
    if not exists:
        return "<字段不存在>"
    if value == "":
        return '""（空字符串）'
    if value is None:
        return "null"
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _request_contract_label(run: dict[str, Any]) -> str:
    """Format the exact method, host, and path contract used for one reported mutation."""
    method = str(run.get("method") or "").upper()
    api = str(run.get("api") or "")
    host = str(run.get("host") or "")
    endpoint = api if "://" in api or not host else f"{host}{api}"
    return " ".join(part for part in (method, endpoint) if part)


def _case_status(runs: list[dict[str, Any]]) -> str:
    """按三端状态计算用例级判定，缺端或待审查时不误报通过。"""
    statuses = [str(run.get("status") or "needs_check") for run in runs]
    targets = {str(run.get("target") or "") for run in runs}
    if "failed" in statuses or "invalid_execution" in statuses:
        return "failed"
    if targets == {"android", "ios", "harmony"} and statuses and all(status == "passed" for status in statuses):
        return "passed"
    return "needs_check"


def _case_conclusion(runs: list[dict[str, Any]]) -> str:
    """Prefer explicit final review conclusions and label algorithmic evidence as precheck-only."""
    for run in runs:
        visual = run.get("visual_review") or {}
        if visual.get("case_conclusion"):
            return str(visual["case_conclusion"])
    notes = list(dict.fromkeys(str(run.get("review_note") or "") for run in runs if run.get("review_note")))
    if notes:
        return "；".join(notes)
    precheck = next((run.get("visual_precheck") or {} for run in runs if run.get("visual_precheck")), {})
    if precheck.get("case_conclusion"):
        return f"视觉预检（非最终结论）：{precheck['case_conclusion']}"
    return "待 VLM 或人工完成最终语义复核"


def _render_visual_metrics(runs: list[dict[str, Any]]) -> str:
    """Render built-in pairwise measurements so the verdict remains auditable."""
    visual = next((run.get("visual_precheck") or {} for run in runs if (run.get("visual_precheck") or {}).get("pairwise")), {})
    metrics = []
    for pair in visual.get("pairwise") or []:
        left = {"android": "Android", "ios": "iOS", "harmony": "HarmonyOS"}.get(str(pair.get("left") or ""), str(pair.get("left") or ""))
        right = {"android": "Android", "ios": "iOS", "harmony": "HarmonyOS"}.get(str(pair.get("right") or ""), str(pair.get("right") or ""))
        status = {"similar": "相似", "different": "明显不同", "needs_check": "灰区"}.get(str(pair.get("status") or ""), str(pair.get("status") or ""))
        metrics.append(
            f'<div class="visual-metric"><strong>{html.escape(left)} vs {html.escape(right)} · {html.escape(status)}</strong><br>'
            f'MAE {float(pair.get("mean_absolute_error") or 0):.3f} · 差异像素 {float(pair.get("pixel_difference_ratio") or 0):.1%} · dHash {int(pair.get("dhash_distance") or 0)}</div>'
        )
    return f'<div class="visual-metrics">{"".join(metrics)}</div>' if metrics else ""


def _status_label(status: str) -> str:
    """把存储状态转换为报告中的中文判定。"""
    return {"passed": "通过", "failed": "失败", "needs_check": "待确认", "pending_review": "待审查", "invalid_execution": "执行无效"}.get(status, "待确认")


def _applied_gate_count(runs: list[dict[str, Any]]) -> int:
    """统计实际完成响应改写的端侧执行数量。"""
    return sum(1 for run in runs if (run.get("execution_gate") or {}).get("change_applied"))


def _write_report_hub(report_root: Path) -> Path:
    """扫描同一报告根目录下的 session，并生成统一可视化入口。"""
    ensure_private_directory(report_root)
    reports: list[dict[str, Any]] = []
    for manifest_path in report_root.glob("*/manifest.json"):
        report_dir = manifest_path.parent
        manifest = _read_json_file(manifest_path)
        summary = _read_json_file(report_dir / "summary.json")
        runs = _read_json_file(report_dir / "runs.json", default=[])
        first_run = runs[0] if isinstance(runs, list) and runs else {}
        reports.append(
            {
                "directory": report_dir.name,
                "session_id": manifest.get("session_id") or summary.get("session_id") or report_dir.name,
                "generated_at": manifest.get("generated_at") or "",
                "records": int(summary.get("records") or 0),
                "passed": int(summary.get("passed") or 0),
                "failed": int(summary.get("failed") or 0),
                "pending": int(summary.get("pending_review") or 0) + int(summary.get("needs_check") or 0),
                "screenshots": int(summary.get("screenshots") or 0),
                "module": str(first_run.get("source_module") or first_run.get("source_feature") or "通用移动端验收"),
            }
        )
    reports.sort(key=lambda item: str(item.get("generated_at") or ""), reverse=True)
    rows = "\n".join(_render_hub_row(item) for item in reports) or '<tr><td colspan="8" class="empty">暂无报告</td></tr>'
    content = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>mobile_auto_mcp 报告中心</title>
  <style>
    :root {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif; --bg:#f4f6f8; --panel:#fff; --line:#d9e0e7; --text:#18212b; --muted:#667085; --link:#175cd3; }}
    * {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--text); font-size:14px; }}
    main {{ max-width:1500px; margin:0 auto; padding:24px; }} header {{ display:flex; justify-content:space-between; align-items:flex-end; gap:20px; margin-bottom:16px; }}
    h1 {{ margin:0 0 6px; font-size:26px; letter-spacing:0; }} .muted {{ color:var(--muted); }} .count {{ font-size:20px; font-weight:750; }}
    .table-wrap {{ overflow:auto; background:var(--panel); border:1px solid var(--line); border-radius:6px; }} table {{ width:100%; min-width:1050px; border-collapse:collapse; }}
    th,td {{ padding:11px 12px; border-bottom:1px solid var(--line); text-align:left; vertical-align:middle; }} th {{ background:#f8fafc; color:#344054; font-size:12px; }} tr:last-child td {{ border-bottom:0; }}
    a {{ color:var(--link); font-weight:700; text-decoration:none; }} a:hover {{ text-decoration:underline; }} code {{ font-size:12px; color:#475467; }}
    .status {{ display:inline-flex; padding:3px 8px; border-radius:999px; font-size:12px; font-weight:700; }} .passed {{ color:#087443; background:#ecfdf3; }} .failed {{ color:#b42318; background:#fef3f2; }} .pending {{ color:#9a6700; background:#fffaeb; }} .empty {{ padding:40px; color:var(--muted); text-align:center; }}
    @media(max-width:760px) {{ main {{ padding:12px; }} header {{ display:block; }} .count {{ margin-top:10px; }} }}
  </style>
</head>
<body><main>
  <header><section><h1>移动端自动化报告中心</h1><div class="muted">统一归档执行证据、三端截图、接口改写记录和视觉结论</div></section><div class="count">{len(reports)} 份报告</div></header>
  <section class="table-wrap"><table><thead><tr><th>报告</th><th>模块</th><th>生成时间</th><th>端侧执行</th><th>通过</th><th>失败</th><th>截图</th><th>状态</th></tr></thead><tbody>{rows}</tbody></table></section>
</main></body></html>"""
    index = report_root / "index.html"
    atomic_write_private_text(index, content)
    atomic_write_private_text(report_root / "reports.json", json.dumps(reports, ensure_ascii=False, indent=2))
    return index


def _render_hub_row(report: dict[str, Any]) -> str:
    """渲染一条报告中心记录，并根据执行摘要给出状态。"""
    if report["failed"]:
        status, label = "failed", "失败"
    elif report["pending"]:
        status, label = "pending", "待审查"
    elif report["records"] and report["passed"] == report["records"]:
        status, label = "passed", "通过"
    else:
        status, label = "pending", "已执行"
    href = html.escape(f"{report['directory']}/index.html", quote=True)
    return (
        "<tr>"
        f'<td><a href="{href}">查看报告</a><br><code>{html.escape(str(report["session_id"]))}</code></td>'
        f'<td>{html.escape(str(report["module"]))}</td>'
        f'<td>{html.escape(str(report["generated_at"]))}</td>'
        f'<td>{report["records"]}</td><td>{report["passed"]}</td><td>{report["failed"]}</td><td>{report["screenshots"]}</td>'
        f'<td><span class="status {status}">{label}</span></td>'
        "</tr>"
    )


def _read_json_file(path: Path, default: Any | None = None) -> Any:
    """容错读取报告元数据，单个损坏 session 不影响整个报告中心。"""
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else ({} if default is None else default)
    except (OSError, json.JSONDecodeError):
        return {} if default is None else default
