"""Tool registration for mobile_auto_mcp."""

from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
import re
import socket
from typing import Any

from mobile_auto_mcp.cases.parser import analyze_case_file, import_case_file
from mobile_auto_mcp.execution.clicker import smart_click as smart_click_impl
from mobile_auto_mcp.execution.devices import DeviceDriver
from mobile_auto_mcp.execution.events import ExecutionEventStore
from mobile_auto_mcp.execution.preflight import run_preflight
from mobile_auto_mcp.execution.readiness import diagnose_environment as diagnose_environment_impl
from mobile_auto_mcp.execution.readiness import prepare_run as prepare_run_impl
from mobile_auto_mcp.execution.runner import run_cases as run_cases_impl
from mobile_auto_mcp.execution.wda_guardian import ensure_wda
from mobile_auto_mcp.proxy.device_proxy import build_device_proxy_adapter
from mobile_auto_mcp.proxy.proxy_manager import ProxyManager
from mobile_auto_mcp.proxy.recovery import ProxyRecoveryManager, WorkspaceBusyError, WorkspaceRunLock
from mobile_auto_mcp.proxy.proxy_trace import build_proxy_trace
from mobile_auto_mcp.reports.reporter import export_archive_report
from mobile_auto_mcp.reports.server import ReportServerManager
from mobile_auto_mcp.reports.visual_compare import apply_session_visual_comparison
from mobile_auto_mcp.state.knowledge import KnowledgeBase
from mobile_auto_mcp.state.storage import LocalStore, workspace_home


def make_store(base_home: str = "", tenant_id: str = "", workspace_id: str = "", app_id: str = "") -> LocalStore:
    """Handle make store using the supplied state and inputs."""
    return LocalStore(workspace_home(base_home=base_home or None, tenant_id=tenant_id, workspace_id=workspace_id, app_id=app_id))


def register_all_tools(mcp: Any) -> None:
    """Register all MCP tools."""

    @mcp.tool()
    def doctor() -> dict[str, Any]:
        """Check whether core Python dependencies are importable."""
        deps: dict[str, Any] = {}
        for name in ("mcp", "mitmproxy", "uiautomator2"):
            try:
                __import__(name)
                deps[name] = True
            except Exception as exc:
                deps[name] = {"ok": False, "error": str(exc)}
        return {"ok": all(value is True for value in deps.values()), "dependencies": deps}

    @mcp.tool()
    def workflow_contract() -> dict[str, Any]:
        """Return the execution contract that prevents false-success runs."""
        return {
            "app_id": "仅作为知识库和运行数据命名空间，不能用于启动 App",
            "app_launch": "Android 必须传 target_app_package；iOS 必须传对应显式 bundle/package 能力字段",
            "proxy": {
                "mutation_allowed": True,
                "policy": "managed_snapshot_apply_verify_retain",
                "required_order": "无 Session readiness → 启动或安全复用 mitmproxy → 快照并设置三端代理 → 读取复核 → 创建正式 Session → 执行 → 保留代理 → 导出报告并提醒用户手动关闭",
                "port_visibility": "preflight、prepare_run、run_cases 必须返回 proxy_instruction，明确 mitmproxy 端口和手机 WLAN 代理地址",
                "mismatch_behavior": "设置后的读取复核不一致时阻断任务，并回滚准备阶段已经修改的设备",
                "host_proof": "电脑代理地址必须是本机候选，并与全部目标手机的新鲜 Wi-Fi 地址处于可证明的共同子网",
                "final_state": "正式执行结束后保留手机 Wi-Fi 代理和 mitmproxy；MCP 返回及报告提醒用户通过 restore_retained_proxy 安全恢复",
            },
            "ui_navigation": {
                "fresh_snapshot": "每次点击前都重新读取当前 UI 元素，不复用旧 list_elements 结果",
                "resolve_before_click": "点击前先按 text/resource_id/content_desc/bounds 解析候选并记录 selected/candidates/confidence",
                "target_gate": "target_page 或显式 target_page_assertions 必须通过页面断言后，才能进入目标接口探针和异常规则",
                "generic_assertions": "页面名不会写死具体业务词；优先使用调用方传入的 target_page_assertions，未传时仅按通用分隔符提取页面名 token",
                "failure_behavior": "找不到元素、页面断言失败、目标接口探针失败时，规则状态写为 invalid_execution",
            },
            "navigation_persistence": {
                "scope": "导航路径默认写入当前 tenant/workspace/app 隔离目录下的 knowledge；调用方可覆盖目录，但无需显式传参",
                "timing": "只要目标页导航和页面断言通过，就记录路径；该动作与 mitmproxy 是否启动解耦",
                "multi_target": "每个设备端独立沉淀路径，禁止把任一端路径作为其他端 fallback",
                "context": "知识键包含 app_id、设备端、target_page 和页面上下文指纹，防止旧业务实体污染新任务",
                "dedupe": "同一完整作用域下路径完全一致时返回 skipped/duplicate，不重复写入",
                "reuse": "后续只复用作用域完全匹配的路径，不做跨端或跨上下文模糊匹配",
            },
            "execution_performance": {
                "transport_contract": "一次 run_cases MCP 调用在同一服务进程内完成三端 Worker，禁止逐动作重启 server",
                "worker_contract": "每个设备端拥有独立 Worker；单端阻塞或失败不暂停其他端",
                "case_gate": "同一用例在全部预期端到达终态后异步封账，并校验请求与修改签名一致性",
                "visual_review": "内置传统算法仅生成 visual_precheck；每端仍必须由 VLM 或人工完成最终语义复核",
                "stage_budget": "前置检查、导航、接口探针、触发、页面校验和截图均执行真实墙钟超时控制",
            },
            "ios_wda": {
                "normal_run": "只复用已就绪 WDA 并恢复 iproxy，禁止自动重装或重签",
                "repair": "只有显式调用 repair_wda 才允许启动 xcodebuild 修复 Runner",
            },
            "traceability": {
                "screenshots": "每条异常规则执行后截图",
                "proxy_logs": "保留 mitmproxy_events.jsonl、trace.json、stdout/stderr 日志路径",
                "navigation_evidence": "导航结果包含 resolver 候选、选中元素、置信度和页面断言结果",
            },
        }

    @mcp.tool()
    def diagnose_environment(target: str = "android") -> dict[str, Any]:
        """Diagnose local tool availability for a target without mutating devices."""
        return diagnose_environment_impl(target=target)

    @mcp.tool()
    def runtime_status(proxy_port: int = 13000, wda_url: str = "") -> dict[str, Any]:
        """Return read-only MCP, proxy, WDA, and device runtime status."""
        try:
            version = importlib.metadata.version("autodevice-mcp")
        except importlib.metadata.PackageNotFoundError:
            version = "source"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            proxy_listening = sock.connect_ex(("127.0.0.1", int(proxy_port))) == 0
        devices = {
            target: run_preflight(target=target, proxy_required=False, wda_url=wda_url if target == "ios" else "").as_dict()
            for target in ("android", "ios", "harmony")
        }
        return {
            "ok": True,
            "version": version,
            "pid": os.getpid(),
            "transport": "stdio",
            "mitmproxy": {"port": int(proxy_port), "listening": proxy_listening},
            "wda": (devices["ios"].get("checks") or {}).get("wda", {}),
            "devices": devices,
        }

    @mcp.tool()
    def execution_events(
        app_id: str = "",
        session_id: str = "",
        lane_id: str = "",
        base_home: str = "",
        data_home: str = "",
        tenant_id: str = "",
        workspace_id: str = "",
        limit: int = 500,
    ) -> dict[str, Any]:
        """Read real-time independent lane stages, retries, failures, and completion events."""
        store = make_store(base_home or data_home, tenant_id, workspace_id, app_id)
        rows = ExecutionEventStore(store.home).read(session_id=session_id, lane_id=lane_id, limit=limit)
        return {"ok": True, "session_id": session_id, "lane_id": lane_id, "events": rows}

    @mcp.tool()
    def prepare_run(
        app_id: str,
        file_path: str = "",
        case_file: str = "",
        target: str = "android",
        package_name: str = "",
        target_app_package: str = "",
        target_app_packages: dict[str, str] | None = None,
        proxy_required: bool = True,
        proxy_port: int | None = None,
        device_serial: str = "",
        android_serial: str = "",
        ios_udid: str = "",
        harmony_serial: str = "",
        wda_url: str = "",
        auto_start_wda: bool = True,
        allow_wda_reinstall: bool = False,
        wda_start_command: str = "",
        wda_iproxy_command: str = "",
    ) -> dict[str, Any]:
        """Prepare a run and return actionable blockers before execution starts."""
        return prepare_run_impl(
            app_id=app_id,
            case_file=case_file or file_path,
            target=target,
            target_app_package=target_app_package or package_name,
            target_app_packages=target_app_packages,
            proxy_required=proxy_required,
            proxy_port=proxy_port,
            device_serial=device_serial or android_serial,
            android_serial=android_serial,
            ios_udid=ios_udid,
            harmony_serial=harmony_serial,
            wda_url=wda_url,
            auto_start_wda=auto_start_wda,
            allow_wda_reinstall=allow_wda_reinstall,
            wda_start_command=wda_start_command,
            wda_iproxy_command=wda_iproxy_command,
        )

    @mcp.tool()
    def preflight(
        target: str = "android",
        proxy_required: bool = True,
        proxy_port: int | None = None,
        device_serial: str = "",
        android_serial: str = "",
        wda_url: str = "",
        auto_start_wda: bool = False,
        allow_wda_reinstall: bool = False,
        wda_start_command: str = "",
        wda_iproxy_command: str = "",
    ) -> dict[str, Any]:
        """Run read-only environment checks before executing a proxy-backed run."""
        return run_preflight(
            target=target,
            proxy_required=proxy_required,
            proxy_port=proxy_port,
            device_serial=device_serial or android_serial,
            wda_url=wda_url,
            auto_start_wda=auto_start_wda,
            allow_wda_reinstall=allow_wda_reinstall,
            wda_start_command=wda_start_command,
            wda_iproxy_command=wda_iproxy_command,
        ).as_dict()

    @mcp.tool()
    def repair_wda(
        device_serial: str = "",
        wda_url: str = "",
        wda_start_command: str = "",
        wda_iproxy_command: str = "",
        timeout: float = 30,
    ) -> dict[str, Any]:
        """Explicitly repair/start WDA; this is the only tool allowed to reinstall or re-sign its runner."""
        return ensure_wda(
            wda_url=wda_url,
            start_command=wda_start_command,
            iproxy_command=wda_iproxy_command,
            device_serial=device_serial,
            allow_wda_reinstall=True,
            timeout=timeout,
        )

    @mcp.tool()
    def device_status(target: str = "android", device_serial: str = "", android_serial: str = "", proxy_port: int | None = None, auto_start_wda: bool = False, wda_url: str = "", wda_start_command: str = "", wda_iproxy_command: str = "") -> dict[str, Any]:
        """Return current foreground app/device state."""
        preflight_result = run_preflight(
            target=target,
            proxy_required=False,
            proxy_port=proxy_port,
            device_serial=device_serial or android_serial,
            wda_url=wda_url,
            auto_start_wda=auto_start_wda,
            wda_start_command=wda_start_command,
            wda_iproxy_command=wda_iproxy_command,
        ).as_dict()
        return {"target": target, "preflight": preflight_result, "current_app": _make_driver(target, device_serial or android_serial, wda_url).current_app()}

    @mcp.tool()
    def analyze_cases(case_file: str) -> dict[str, Any]:
        """Analyze a testcase markdown file without writing rules."""
        return analyze_case_file(case_file)

    @mcp.tool()
    def extract_cases(case_file: str) -> dict[str, Any]:
        """Alias of analyze_cases for compatibility."""
        return analyze_case_file(case_file)

    @mcp.tool()
    def import_cases(
        file_path: str = "",
        case_file: str = "",
        knowledge_dir: str = "",
        keyword: str = "",
        app_id: str = "",
        data_home: str = "",
        base_home: str = "",
        tenant_id: str = "",
        workspace_id: str = "",
    ) -> dict[str, Any]:
        """Import testcase markdown into the app namespace."""
        return import_case_file(make_store(base_home or data_home, tenant_id, workspace_id, app_id), case_file or file_path, knowledge_dir)

    @mcp.tool()
    def list_rules(app_id: str = "", keyword: str = "", enabled_only: bool = True, base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> list[dict[str, Any]]:
        """List stored abnormal rules."""
        return make_store(base_home or data_home, tenant_id, workspace_id, app_id).list_rules(keyword=keyword, enabled_only=enabled_only)

    @mcp.tool()
    def rule_preview(rule_id: str, app_id: str = "", base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Return one rule by id."""
        for rule in make_store(base_home or data_home, tenant_id, workspace_id, app_id).list_rules(enabled_only=False):
            if rule.get("id") == rule_id:
                return rule
        return {}

    @mcp.tool()
    def apply_case_asset_overrides(
        rule_ids: list[str],
        app_id: str = "",
        api_override: str = "",
        api_overrides: dict[str, str] | None = None,
        host_override: str = "",
        host_overrides: dict[str, str] | None = None,
        method_override: str = "",
        method_overrides: dict[str, str] | None = None,
        patches: list[dict[str, Any]] | None = None,
        fixtures: list[dict[str, Any]] | None = None,
        mock_sources: list[dict[str, Any]] | None = None,
        base_home: str = "",
        data_home: str = "",
        tenant_id: str = "",
        workspace_id: str = "",
    ) -> dict[str, Any]:
        """Update exact request contracts or merge fixtures/mock assets into existing rules."""
        store = make_store(base_home or data_home, tenant_id, workspace_id, app_id)
        contract_updated = store.update_rule_request_contracts(
            rule_ids,
            api_override=api_override,
            api_overrides=api_overrides,
            host_override=host_override,
            host_overrides=host_overrides,
            method_override=method_override,
            method_overrides=method_overrides,
        )
        asset_updated = store.merge_rule_assets(rule_ids, patches=patches, fixtures=fixtures, mock_sources=mock_sources)
        return {"contract_updated": contract_updated, "api_updated": contract_updated, "asset_updated": asset_updated}

    @mcp.tool()
    def start_run(app_id: str, target: str = "android", rule_ids: list[str] | None = None, base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Create a run session manually."""
        store = make_store(base_home or data_home, tenant_id, workspace_id, app_id)
        ids = rule_ids or [rule["id"] for rule in store.list_rules()]
        return store.start_session(target, ids)

    @mcp.tool()
    def record_result(session_id: str, rule_id: str, app_id: str, target: str = "android", status: str = "pending_review", screenshot: str = "", review_note: str = "", base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Record a manual run result."""
        return make_store(base_home or data_home, tenant_id, workspace_id, app_id).record_run_result(session_id, target, rule_id, status=status, screenshot=screenshot, review_note=review_note)

    @mcp.tool()
    def get_run_status(session_id: str, app_id: str = "", base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Summarize a run session."""
        store = make_store(base_home or data_home, tenant_id, workspace_id, app_id)
        return {"summary": store.session_summary(session_id), "runs": store.list_runs(session_id)}

    @mcp.tool()
    def export_report(session_id: str, app_id: str = "", report_dir: str = "", base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Export report bundle for a session."""
        store = make_store(base_home or data_home, tenant_id, workspace_id, app_id)
        return export_archive_report(store, session_id, Path(report_dir).expanduser() if report_dir else None)

    @mcp.tool()
    # LAN sharing is caller-configurable but never enabled implicitly.
    def start_report_server(app_id: str = "", host: str = "127.0.0.1", port: int = 13080, base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Start a persistent loopback report server; pass a LAN host explicitly to share."""
        store = make_store(base_home or data_home, tenant_id, workspace_id, app_id)
        return ReportServerManager(store.home / "reports", host=host, port=port).start()

    @mcp.tool()
    def report_server_status(app_id: str = "", host: str = "127.0.0.1", port: int = 13080, base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Return report hub server state and access URLs."""
        store = make_store(base_home or data_home, tenant_id, workspace_id, app_id)
        return ReportServerManager(store.home / "reports", host=host, port=port).status()

    @mcp.tool()
    def stop_report_server(app_id: str = "", host: str = "127.0.0.1", port: int = 13080, base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Stop the persistent report hub server owned by this app workspace."""
        store = make_store(base_home or data_home, tenant_id, workspace_id, app_id)
        return ReportServerManager(store.home / "reports", host=host, port=port).stop()

    @mcp.tool()
    def latest_report(app_id: str = "", base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Return latest report path when available."""
        store = make_store(base_home or data_home, tenant_id, workspace_id, app_id)
        reports = sorted((store.home / "reports").glob("*/report.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        return {"report": str(reports[0]) if reports else ""}

    @mcp.tool()
    def proxy_trace(app_id: str = "", session_id: str = "", base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Inspect recent proxy requests and hits."""
        return build_proxy_trace(make_store(base_home or data_home, tenant_id, workspace_id, app_id), session_id=session_id)

    @mcp.tool()
    def run_diagnosis(app_id: str = "", session_id: str = "", base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Alias of proxy_trace with run summary."""
        store = make_store(base_home or data_home, tenant_id, workspace_id, app_id)
        return {"summary": store.session_summary(session_id) if session_id else {}, "proxy": build_proxy_trace(store, session_id=session_id)}

    @mcp.tool()
    def run_cases(
        app_id: str,
        file_path: str = "",
        case_file: str = "",
        knowledge_dir: str = "",
        target: str = "android",
        target_page: str = "",
        package_name: str = "",
        target_app_package: str = "",
        target_app_packages: dict[str, str] | None = None,
        navigation_path: list[dict[str, Any]] | None = None,
        navigation_paths: dict[str, list[dict[str, Any]]] | None = None,
        request_trigger_path: list[dict[str, Any]] | None = None,
        request_trigger_paths: dict[str, list[dict[str, Any]]] | None = None,
        navigation_context: str = "",
        target_page_assertions: list[dict[str, Any]] | None = None,
        tenant_id: str = "",
        workspace_id: str = "",
        data_home: str = "",
        base_home: str = "",
        rule_keyword: str = "",
        keyword: str = "",
        rule_ids: list[str] | None = None,
        proxy_required: bool = True,
        proxy_port: int | None = None,
        proxy_host: str = "",
        skip_preflight: bool = False,
        device_serial: str = "",
        android_serial: str = "",
        ios_udid: str = "",
        harmony_serial: str = "",
        wda_url: str = "",
        auto_start_wda: bool = False,
        allow_wda_reinstall: bool = False,
        wda_start_command: str = "",
        wda_iproxy_command: str = "",
        stage_budgets: dict[str, float] | None = None,
        auto_visual_compare: bool = True,
    ) -> dict[str, Any]:
        """一次 MCP 调用内完成 readiness、三端 Worker 和证据报告；保留代理并提醒用户手动关闭。"""
        return run_cases_impl(
            app_id=app_id,
            case_file=case_file or file_path,
            knowledge_dir=knowledge_dir,
            target=target,
            target_page=target_page,
            target_app_package=target_app_package or package_name,
            target_app_packages=target_app_packages,
            navigation_path=navigation_path,
            navigation_paths=navigation_paths,
            request_trigger_path=request_trigger_path,
            request_trigger_paths=request_trigger_paths,
            navigation_context=navigation_context,
            target_page_assertions=target_page_assertions,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            base_home=base_home or data_home,
            rule_keyword=rule_keyword or keyword,
            rule_ids=rule_ids,
            proxy_required=proxy_required,
            proxy_port=proxy_port,
            proxy_host=proxy_host,
            skip_preflight=skip_preflight,
            device_serial=device_serial,
            android_serial=android_serial,
            ios_udid=ios_udid,
            harmony_serial=harmony_serial,
            wda_url=wda_url,
            auto_start_wda=auto_start_wda,
            allow_wda_reinstall=allow_wda_reinstall,
            wda_start_command=wda_start_command,
            wda_iproxy_command=wda_iproxy_command,
            stage_budgets=stage_budgets,
            auto_visual_compare=auto_visual_compare,
        )

    @mcp.tool()
    def restore_retained_proxy(
        app_id: str = "",
        base_home: str = "",
        data_home: str = "",
        tenant_id: str = "",
        workspace_id: str = "",
        wda_url: str = "",
    ) -> dict[str, Any]:
        """Restore every persisted device proxy snapshot and stop only the verified owned mitmproxy process."""
        store = make_store(base_home or data_home, tenant_id, workspace_id, app_id)
        lock = WorkspaceRunLock(store.home, owner="restore_retained_proxy")
        try:
            lock.acquire()
        except WorkspaceBusyError as exc:
            return {"pending": {}, "result": {"ok": False, "status": "workspace_busy", "message": str(exc)}, "reports": {}}
        try:
            recovery = ProxyRecoveryManager(store.home)
            pending = recovery.load_pending()
            drivers: dict[str, DeviceDriver] = {}

            def adapter_factory(target: str, device_serial: str) -> Any:
                """Recreate a fresh platform adapter from durable target and serial evidence."""
                driver = None
                if target != "android":
                    driver = drivers.setdefault(target, _make_driver(target, device_serial, wda_url))
                return build_device_proxy_adapter(target, device_serial, driver)

            result = recovery.restore_and_stop(
                adapter_factory=adapter_factory,
                proxy_stopper=ProxyManager.stop_owned_runtime,
            )
            session_ids = [
                str(value)
                for value in ((pending or {}).get("session_ids") or [(pending or {}).get("session_id")])
                if value
            ]
            reports: dict[str, Any] = {}
            for session_id in session_ids:
                if not store.get_session(session_id):
                    continue
                store.update_session_metadata(
                    session_id,
                    proxy_cleanup={"requested": True, **result},
                    proxy_lifecycle={
                        "status": "restored" if result.get("ok") else "cleanup_failed",
                        "verified": bool(result.get("ok")),
                        "manual_cleanup_required": not bool(result.get("ok")),
                        "auto_stop": False,
                    },
                )
                reports[session_id] = export_archive_report(
                    store,
                    session_id,
                    report_dir=store.home / "reports" / session_id,
                )
            return {"pending": pending or {}, "result": result, "reports": reports}
        finally:
            lock.release()

    @mcp.tool()
    def launch_app(target_app_package: str = "", package_name: str = "", target: str = "android", device_serial: str = "", android_serial: str = "", wait_seconds: float = 3, wda_url: str = "", auto_start_wda: bool = False, wda_start_command: str = "", wda_iproxy_command: str = "") -> dict[str, Any]:
        """Launch an app by explicit package/bundle id."""
        return _make_driver(target, device_serial or android_serial, wda_url, auto_start_wda=auto_start_wda, wda_start_command=wda_start_command, wda_iproxy_command=wda_iproxy_command).launch_app(target_app_package or package_name, wait_seconds=wait_seconds)

    @mcp.tool()
    def get_current_app(target: str = "android", device_serial: str = "", android_serial: str = "", wda_url: str = "", auto_start_wda: bool = False, wda_start_command: str = "", wda_iproxy_command: str = "") -> dict[str, Any]:
        """Return the current foreground app."""
        return _make_driver(target, device_serial or android_serial, wda_url, auto_start_wda=auto_start_wda, wda_start_command=wda_start_command, wda_iproxy_command=wda_iproxy_command).current_app()

    @mcp.tool()
    def list_elements(target: str = "android", device_serial: str = "", android_serial: str = "", limit: int = 80, wda_url: str = "", auto_start_wda: bool = False, wda_start_command: str = "", wda_iproxy_command: str = "") -> list[dict[str, Any]]:
        """List visible UI elements."""
        return _make_driver(target, device_serial or android_serial, wda_url, auto_start_wda=auto_start_wda, wda_start_command=wda_start_command, wda_iproxy_command=wda_iproxy_command).list_elements(limit=limit)

    @mcp.tool()
    def click(
        target: str = "android",
        device_serial: str = "",
        android_serial: str = "",
        text: str = "",
        resource_id: str = "",
        method: str = "",
        value: str = "",
        bounds: list[int] | None = None,
        x: int | None = None,
        y: int | None = None,
        x_percent: float | None = None,
        y_percent: float | None = None,
        wda_url: str = "",
        ios_tap_backend: str = "",
        ios_tap_command: str = "",
        auto_start_wda: bool = False,
        wda_start_command: str = "",
        wda_iproxy_command: str = "",
    ) -> dict[str, Any]:
        """Click by text, resource id, or coordinates."""
        return _click_compat(_make_driver(target, device_serial or android_serial, wda_url, ios_tap_backend, ios_tap_command, auto_start_wda=auto_start_wda, wda_start_command=wda_start_command, wda_iproxy_command=wda_iproxy_command), text, resource_id, method, value, bounds, x, y, x_percent, y_percent)

    @mcp.tool()
    def swipe(
        target: str = "android",
        device_serial: str = "",
        android_serial: str = "",
        x1: int | None = None,
        y1: int | None = None,
        x2: int | None = None,
        y2: int | None = None,
        x1_percent: float | None = None,
        y1_percent: float | None = None,
        x2_percent: float | None = None,
        y2_percent: float | None = None,
        duration: float = 0.2,
        wda_url: str = "",
        auto_start_wda: bool = False,
        wda_start_command: str = "",
        wda_iproxy_command: str = "",
    ) -> dict[str, Any]:
        """Swipe using absolute coordinates or percentages of the live device window."""
        driver = _make_driver(
            target,
            device_serial or android_serial,
            wda_url,
            auto_start_wda=auto_start_wda,
            wda_start_command=wda_start_command,
            wda_iproxy_command=wda_iproxy_command,
        )
        return _swipe_compat(
            driver,
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            x1_percent=x1_percent,
            y1_percent=y1_percent,
            x2_percent=x2_percent,
            y2_percent=y2_percent,
            duration=duration,
        )

    @mcp.tool()
    def smart_click(
        target: str = "android",
        device_serial: str = "",
        android_serial: str = "",
        text: str = "",
        resource_id: str = "",
        target_description: str = "",
        x: int | None = None,
        y: int | None = None,
        x_percent: float | None = None,
        y_percent: float | None = None,
        vision_command: str = "",
        allow_tree: bool | None = None,
        allow_wda_fallback: bool = False,
        screenshot_path: str = "",
        wda_url: str = "",
        ios_tap_backend: str = "",
        ios_tap_command: str = "",
        auto_start_wda: bool = False,
        wda_start_command: str = "",
        wda_iproxy_command: str = "",
    ) -> dict[str, Any]:
        """Resolve and click with element-tree, visual layout, or external vision fallback."""
        return smart_click_impl(
            _make_driver(target, device_serial or android_serial, wda_url, ios_tap_backend, ios_tap_command, auto_start_wda=auto_start_wda, wda_start_command=wda_start_command, wda_iproxy_command=wda_iproxy_command),
            text=text,
            resource_id=resource_id,
            target_description=target_description,
            x=x,
            y=y,
            x_percent=x_percent,
            y_percent=y_percent,
            vision_command=vision_command,
            allow_tree=allow_tree,
            allow_wda_fallback=allow_wda_fallback,
            screenshot_path=screenshot_path,
        )

    @mcp.tool()
    def intent_click(
        target: str = "android",
        device_serial: str = "",
        android_serial: str = "",
        instruction: str = "",
        vision_command: str = "",
        allow_tree: bool | None = None,
        allow_wda_fallback: bool = False,
        screenshot_path: str = "",
        wda_url: str = "",
        ios_tap_backend: str = "",
        ios_tap_command: str = "",
        auto_start_wda: bool = False,
        wda_start_command: str = "",
        wda_iproxy_command: str = "",
    ) -> dict[str, Any]:
        """Execute a click directly from a natural language instruction."""
        instruction = instruction.strip()
        if not instruction:
            return {"ok": False, "reason": "instruction_required"}

        target_text = _normalize_click_instruction(instruction)
        if not target_text:
            return {"ok": False, "reason": "instruction_unresolvable", "instruction": instruction}

        return smart_click_impl(
            _make_driver(target, device_serial or android_serial, wda_url, ios_tap_backend, ios_tap_command, auto_start_wda=auto_start_wda, wda_start_command=wda_start_command, wda_iproxy_command=wda_iproxy_command),
            text=target_text,
            target_description=instruction,
            vision_command=vision_command,
            allow_tree=allow_tree,
            allow_wda_fallback=allow_wda_fallback,
            screenshot_path=screenshot_path,
        )

    @mcp.tool()
    def screenshot(path: str, target: str = "android", device_serial: str = "", android_serial: str = "", wda_url: str = "", auto_start_wda: bool = False, wda_start_command: str = "", wda_iproxy_command: str = "") -> dict[str, Any]:
        """Capture a device screenshot."""
        return {"path": _make_driver(target, device_serial or android_serial, wda_url, auto_start_wda=auto_start_wda, wda_start_command=wda_start_command, wda_iproxy_command=wda_iproxy_command).screenshot(path)}

    @mcp.tool()
    def navigate_path(path: list[dict[str, Any]], target: str = "android", device_serial: str = "", android_serial: str = "", wda_url: str = "", auto_start_wda: bool = False, wda_start_command: str = "", wda_iproxy_command: str = "") -> dict[str, Any]:
        """Execute a navigation path."""
        return _make_driver(target, device_serial or android_serial, wda_url, auto_start_wda=auto_start_wda, wda_start_command=wda_start_command, wda_iproxy_command=wda_iproxy_command).navigate_path(path)

    @mcp.tool()
    def record_navigation_path(knowledge_dir: str, app_id: str, target_page: str, path: list[dict[str, Any]], target: str = "", context: str = "") -> dict[str, Any]:
        """Persist a reusable navigation path."""
        return KnowledgeBase(knowledge_dir).record_navigation_path(app_id, target_page, path, target=target, context=context)

    @mcp.tool()
    def suggest_navigation_path(knowledge_dir: str, app_id: str, target_page: str, target: str = "", context: str = "") -> dict[str, Any]:
        """Suggest a saved navigation path."""
        return KnowledgeBase(knowledge_dir).suggest_navigation_path(app_id, target_page, target=target, context=context)

    @mcp.tool()
    def page_knowledge(knowledge_dir: str, app_id: str, target_page: str = "", page_key: str = "", target: str = "", context: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Return page-level knowledge."""
        return KnowledgeBase(knowledge_dir or data_home).suggest_navigation_path(app_id, target_page or page_key, target=target, context=context)

    @mcp.tool()
    def record_field_alias(knowledge_dir: str, app_id: str, alias: str, field: str) -> dict[str, Any]:
        """Persist field alias knowledge."""
        return KnowledgeBase(knowledge_dir).record_field_alias(app_id, alias, field)

    @mcp.tool()
    def suggest_field_alias(knowledge_dir: str, app_id: str, alias: str) -> dict[str, Any]:
        """Suggest a field path from alias knowledge."""
        return KnowledgeBase(knowledge_dir).suggest_field_alias(app_id, alias)

    @mcp.tool()
    def review_session(session_id: str, app_id: str = "", base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Return review candidates for a session."""
        store = make_store(base_home or data_home, tenant_id, workspace_id, app_id)
        return {"summary": store.session_summary(session_id), "runs": store.list_runs(session_id)}

    @mcp.tool()
    def apply_manual_reviews(reviews: list[dict[str, Any]], app_id: str = "", base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Apply human review statuses."""
        store = make_store(base_home or data_home, tenant_id, workspace_id, app_id)
        updated = [store.update_manual_review(item["run_id"], item["status"], item.get("review_note", ""), item.get("reviewer", "human")) for item in reviews]
        sessions = _refresh_reviewed_sessions(store, updated)
        return {"updated": updated, "sessions": sessions}

    @mcp.tool()
    def prepare_visual_review(session_id: str, app_id: str = "", base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Return screenshots and metadata ready for visual review."""
        store = make_store(base_home or data_home, tenant_id, workspace_id, app_id)
        return {"items": _visual_review_items(store.list_runs(session_id))}

    @mcp.tool()
    def run_visual_comparison(session_id: str, app_id: str = "", expected_targets: list[str] | None = None, base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Persist a non-final built-in screenshot precheck and refresh the evidence report."""
        store = make_store(base_home or data_home, tenant_id, workspace_id, app_id)
        comparison = apply_session_visual_comparison(store, session_id, expected_targets=expected_targets)
        report = export_archive_report(store, session_id, report_dir=store.home / "reports" / session_id)
        return {"comparison": comparison, "report": report}

    @mcp.tool()
    def apply_visual_reviews(reviews: list[dict[str, Any]], app_id: str = "", base_home: str = "", data_home: str = "", tenant_id: str = "", workspace_id: str = "") -> dict[str, Any]:
        """Apply final semantic visual review results and recompute affected session outcomes."""
        store = make_store(base_home or data_home, tenant_id, workspace_id, app_id)
        updated = [store.update_run_review(item["run_id"], item.get("status", "needs_check"), item.get("review_note", ""), item) for item in reviews]
        sessions = _refresh_reviewed_sessions(store, updated)
        return {"updated": updated, "sessions": sessions}


def _refresh_reviewed_sessions(store: LocalStore, updated_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recompute and export every session affected by final human or semantic review decisions."""
    sessions: list[dict[str, Any]] = []
    for session_id in dict.fromkeys(str(run.get("session_id") or "") for run in updated_runs):
        if not session_id:
            continue
        session = store.refresh_session_review_status(session_id)
        report = export_archive_report(store, session_id, report_dir=store.home / "reports" / session_id)
        sessions.append({"session": session, "report": report})
    return sessions


def _visual_review_items(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclude invalid executions from semantic visual review."""
    items: list[dict[str, Any]] = []
    for run in runs:
        gate = run.get("execution_gate") or {}
        page_anchor = gate.get("page_anchor") or {}
        if run.get("status") != "pending_review" or not run.get("screenshot"):
            continue
        if not (
            gate.get("change_applied")
            and page_anchor.get("ok")
            and page_anchor.get("verified")
            and not page_anchor.get("skipped")
        ):
            continue
        items.append(
            {
                "run_id": run["id"],
                "screenshot": run.get("screenshot"),
                "case_name": run.get("case_name"),
                "target": run.get("target"),
                "traceability": run.get("traceability") or {},
            }
        )
    return items


def _click_compat(
    driver: DeviceDriver,
    text: str = "",
    resource_id: str = "",
    method: str = "",
    value: str = "",
    bounds: list[int] | None = None,
    x: int | None = None,
    y: int | None = None,
    x_percent: float | None = None,
    y_percent: float | None = None,
) -> dict[str, Any]:
    """Translate older click schemas into the current driver call."""
    normalized = (method or "").lower()
    if normalized == "text":
        text = text or value
    elif normalized in {"id", "resource_id"}:
        resource_id = resource_id or value
    elif normalized == "bounds" and bounds and len(bounds) >= 4:
        x = (int(bounds[0]) + int(bounds[2])) // 2
        y = (int(bounds[1]) + int(bounds[3])) // 2
    elif normalized in {"coords", "coordinate", "xy"} and value and "," in value:
        left, right = value.split(",", 1)
        x, y = int(float(left)), int(float(right))
    elif normalized == "percent" and x_percent is not None and y_percent is not None:
        device = driver.connect()
        if device is not None:
            width, height = device.window_size()
            x, y = int(width * x_percent), int(height * y_percent)
    return driver.click(text=text, resource_id=resource_id, x=x, y=y)


def _swipe_compat(
    driver: DeviceDriver,
    x1: int | None = None,
    y1: int | None = None,
    x2: int | None = None,
    y2: int | None = None,
    x1_percent: float | None = None,
    y1_percent: float | None = None,
    x2_percent: float | None = None,
    y2_percent: float | None = None,
    duration: float = 0.2,
) -> dict[str, Any]:
    """Delegate a swipe while keeping percentage conversion inside the driver."""
    if any(value is None for value in (x1, y1, x2, y2)) and not all(
        value is not None for value in (x1_percent, y1_percent, x2_percent, y2_percent)
    ):
        return {"ok": False, "reason": "swipe_coordinates_required"}
    return driver.swipe(
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        duration=duration,
        x1_percent=x1_percent,
        y1_percent=y1_percent,
        x2_percent=x2_percent,
        y2_percent=y2_percent,
    )


def _make_driver(
    target: str,
    device_serial: str = "",
    wda_url: str = "",
    ios_tap_backend: str = "",
    ios_tap_command: str = "",
    *,
    auto_start_wda: bool = False,
    wda_start_command: str = "",
    wda_iproxy_command: str = "",
) -> DeviceDriver:
    """Create driver using the supplied state and inputs."""
    if target.lower() == "ios":
        return DeviceDriver(
            target=target,
            device_serial=device_serial,
            wda_url=wda_url,
            ios_tap_backend=ios_tap_backend,
            ios_tap_command=ios_tap_command,
            auto_start_wda=auto_start_wda,
            wda_start_command=wda_start_command,
            wda_iproxy_command=wda_iproxy_command,
        )
    return DeviceDriver(target=target, device_serial=device_serial)


def _normalize_click_instruction(instruction: str) -> str:
    """Normalize click instruction using the supplied state and inputs."""
    text = (instruction or "").strip()
    if not text:
        return ""
    text = re.sub(r"^请(?:你)?(?:帮我|帮)?(?:帮我)?", "", text).strip()
    text = re.sub(r"^(?:[，,]?)(?:打开|打开一下|点击|点击一下|点开|点一下|进入|切到|切换到|前往|去到|去)[\\s　]*", "", text)
    text = re.sub(r"^(?:请)?(?:先)?(?:先)?(?:点|点击|进入|打开|切换到|前往)?", "", text).strip()
    text = re.sub(r"(页面|入口|标签页|tab|按钮|栏目)?(?:吧|吧界面)?$", "", text).strip()
    return text
