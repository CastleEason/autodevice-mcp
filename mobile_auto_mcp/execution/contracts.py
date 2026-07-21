"""Pure execution safety contracts shared by runners and MCP entry points."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def validate_execution_contract(
    rules: list[dict[str, Any]],
    *,
    requested_rule_ids: list[str] | None = None,
    target_page: str = "",
    target_page_assertions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate that a run has explicit rules, APIs, and a verifiable page anchor.

    The returned ``blockers`` are stable machine-readable failures.  Callers must
    stop before preflight or device mutation whenever ``ok`` is false.
    """
    blockers: list[dict[str, Any]] = []
    selected_rules = [
        rule for rule in rules if isinstance(rule, dict) and rule.get("enabled", True)
    ]
    selected_ids = {
        str(rule.get("id") or "") for rule in selected_rules if rule.get("id")
    }
    requested_ids = {
        str(rule_id).strip()
        for rule_id in (requested_rule_ids or [])
        if str(rule_id).strip()
    }

    if not selected_rules:
        blockers.append(
            {
                "code": "execution_rules_required",
                "message": "至少需要一条已启用的有效规则，禁止执行空规则会话。",
            }
        )

    missing_ids = sorted(requested_ids - selected_ids)
    if missing_ids:
        # 拒绝静默丢失调用方指定的规则，防止“请求执行 A，实际执行 B”仍被报告成功。
        blockers.append(
            {
                "code": "requested_rules_missing",
                "message": "部分指定规则不存在或未启用，禁止降级为其他规则继续执行。",
                "rule_ids": missing_ids,
            }
        )

    for index, rule in enumerate(selected_rules):
        rule_id = str(rule.get("id") or "")
        if not rule_id:
            blockers.append(
                {
                    "code": "rule_id_required",
                    "message": "自动执行规则必须具有稳定规则 ID。",
                    "rule_index": index,
                }
            )
        api = str(rule.get("api") or "").strip()
        if not api:
            # 没有 API 的规则会退化成匹配任意 JSON 响应，必须在代理启动前硬阻断。
            blockers.append(
                {
                    "code": "rule_api_required",
                    "message": "每条自动改写规则必须配置明确的目标 API。",
                    "rule_id": rule_id,
                    "rule_index": index,
                }
            )
        api_parts = urlsplit(api)
        configured_host = str(rule.get("host") or api_parts.hostname or "").strip()
        if api and not configured_host:
            blockers.append(
                {
                    "code": "rule_host_required",
                    "message": "每条自动改写规则必须配置精确 Host，禁止 Path-only 通配。",
                    "rule_id": rule_id,
                    "rule_index": index,
                }
            )
        if not str(rule.get("method") or rule.get("http_method") or "").strip():
            blockers.append(
                {
                    "code": "rule_method_required",
                    "message": "每条自动改写规则必须配置精确 HTTP Method。",
                    "rule_id": rule_id,
                    "rule_index": index,
                }
            )
        if not _has_executable_mutation_asset(rule):
            # 只有接口没有任何改写资产属于探针用例，不能冒充自动异常响应执行规则。
            blockers.append(
                {
                    "code": "rule_mutation_required",
                    "message": (
                        "每条自动执行规则必须至少配置一项 mutation、patch、"
                        "fixture 或 mock source。"
                    ),
                    "rule_id": rule_id,
                    "rule_index": index,
                }
            )

    assertions = [
        item
        for item in (target_page_assertions or [])
        if isinstance(item, dict) and item
    ]
    if not str(target_page or "").strip() and not assertions:
        # 页面锚点证明改写后截图仍位于目标业务页，缺失时不得进入成功链路。
        blockers.append(
            {
                "code": "page_anchor_required",
                "message": "必须配置目标页面或页面断言，禁止无页面锚点执行自动判定。",
            }
        )

    return {
        "ok": not blockers,
        "status": "ready" if not blockers else "invalid_execution_contract",
        "blockers": blockers,
        "valid_rules": selected_rules if not blockers else [],
        "selected_rule_ids": sorted(selected_ids),
        "requested_rule_ids": sorted(requested_ids),
    }


def _has_executable_mutation_asset(rule: dict[str, Any]) -> bool:
    """Require at least one asset and reject every malformed item in any supplied asset list."""
    supported_mutations = {
        "missing",
        "empty",
        "empty_array",
        "empty_object",
        "long_text",
        "emoji",
        "special_char",
        "image_unreachable",
    }
    validators: list[tuple[list[Any], Any]] = []

    def valid_mutation(item: Any) -> bool:
        """Validate one supported field mutation instruction."""
        if not isinstance(item, dict):
            return False
        field = str(item.get("field") or item.get("path") or "").strip()
        return bool(field and str(item.get("action") or "").strip() in supported_mutations)

    def valid_patch(item: Any) -> bool:
        """Validate one patch instruction accepted by the patch engine."""
        action = str(item.get("action") or "upsert") if isinstance(item, dict) else ""
        return bool(
            isinstance(item, dict)
            and str(item.get("field") or "").strip()
            and action in {"upsert", "replace", "deep_merge"}
        )

    def valid_fixture(item: Any) -> bool:
        """Validate one fixture that can merge at its configured path or object root."""
        return bool(
            isinstance(item, dict)
            and "value" in item
            and (str(item.get("merge_path") or "").strip() or isinstance(item.get("value"), dict))
        )

    def valid_mock_source(item: Any) -> bool:
        """Validate one non-null mock response source."""
        return bool(
            isinstance(item, dict)
            and any(key in item and item.get(key) is not None for key in ("value", "payload", "body"))
        )

    validators.extend(
        [
            (list(rule.get("mutations") or []), valid_mutation),
            (list(rule.get("patches") or []), valid_patch),
            (list(rule.get("fixtures") or []), valid_fixture),
            (list(rule.get("mock_sources") or []), valid_mock_source),
        ]
    )
    supplied = [(items, validator) for items, validator in validators if items]
    return bool(supplied) and all(all(validator(item) for item in items) for items, validator in supplied)


def derive_single_run_status(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive the top-level single-device result from persisted per-rule evidence.

    A run is complete only when every rule has a valid request mutation and page
    anchor.  Review states remain report work; invalid execution can never be
    converted into ``finished`` merely because the runner reached its final line.
    """
    blockers: list[dict[str, Any]] = []
    if not runs:
        blockers.append(
            {
                "code": "run_results_required",
                "message": "没有生成任何规则执行结果，禁止报告执行完成。",
            }
        )

    for run in runs:
        rule_id = str(run.get("rule_id") or "")
        status = str(run.get("status") or "")
        gate = run.get("execution_gate") or {}
        page_anchor = gate.get("page_anchor") or {}
        if status in {"invalid_execution", "failed", "blocked"}:
            blockers.append(
                {
                    "code": "rule_execution_invalid",
                    "message": "至少一条规则执行失败或执行证据无效。",
                    "rule_id": rule_id,
                    "run_status": status,
                }
            )
            continue
        if not bool(gate.get("change_applied")):
            blockers.append(
                {
                    "code": "response_change_unproven",
                    "message": "没有证据证明目标响应已实际改写。",
                    "rule_id": rule_id,
                }
            )
        anchor_proven = (
            bool(page_anchor.get("ok"))
            and bool(page_anchor.get("verified"))
            and not bool(page_anchor.get("skipped"))
        )
        if not anchor_proven:
            # 即使截图存在，也不能把未知页面或被跳过的锚点验证视为业务成功。
            blockers.append(
                {
                    "code": "page_anchor_unproven",
                    "message": "目标页面锚点未通过，不能确认截图属于预期业务页面。",
                    "rule_id": rule_id,
                }
            )

    execution_ok = not blockers
    return {
        # 自动执行证据有效也仍待最终语义复核，因此顶层 ok 不能提前代表业务通过。
        "ok": False,
        "execution_ok": execution_ok,
        "status": "awaiting_review" if execution_ok else "partial",
        "blockers": blockers,
        "run_count": len(runs),
    }
