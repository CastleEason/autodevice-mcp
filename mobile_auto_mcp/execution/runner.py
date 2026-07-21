"""Full-chain abnormal rule execution."""

from __future__ import annotations

import time
import json
import re
import threading
import inspect
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from uuid import uuid4

from mobile_auto_mcp.execution.devices import DeviceDriver
from mobile_auto_mcp.execution.contracts import derive_single_run_status, validate_execution_contract
from mobile_auto_mcp.execution.events import ExecutionEventStore, LaneStageTimeout, LaneStateMachine
from mobile_auto_mcp.execution.failures import build_failure
from mobile_auto_mcp.execution.preflight import PreflightResult, build_proxy_instruction, run_preflight
from mobile_auto_mcp.execution.probe import LaneProbeCoordinator
from mobile_auto_mcp.proxy.device_proxy import ManagedProxyLease, build_device_proxy_adapter
from mobile_auto_mcp.proxy.proxy_manager import ProxyManager
from mobile_auto_mcp.reports.reporter import export_archive_report
from mobile_auto_mcp.reports.visual_compare import apply_session_visual_comparison
from mobile_auto_mcp.state.knowledge import KnowledgeBase
from mobile_auto_mcp.state.storage import LocalStore, workspace_home


DEFAULT_STAGE_BUDGETS: dict[str, float] = {
    "preflight": 20,
    "navigation": 30,
    "target_api_probe": 12,
    "rule": 45,
    "trigger": 20,
    "await_hit": 12,
    "verify_page": 8,
    "capture": 30,
}

MANUAL_PROXY_REMINDER = "代理会继续保留；不再抓包时请自行调用 restore_retained_proxy，确认手机原代理已恢复且本次 owned mitmproxy 已停止。"


def prepare_managed_environment(
    *,
    store: LocalStore,
    targets: list[str],
    device_serials: dict[str, str],
    proxy_required: bool,
    proxy_port: int | None,
    proxy_host: str = "",
    wda_url: str,
    auto_start_wda: bool,
    allow_wda_reinstall: bool,
    wda_start_command: str,
    wda_iproxy_command: str,
    stage_budgets: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Complete preflight, proxy startup, device proxy acquisition, and readback before a formal Session exists."""
    readiness_id = f"readiness-{uuid4().hex}"
    budgets = _resolve_stage_budgets(stage_budgets, DEFAULT_STAGE_BUDGETS["target_api_probe"])
    event_store = ExecutionEventStore(store.home)
    machines = {lane: LaneStateMachine(event_store, readiness_id, lane) for lane in targets}
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as executor:
        futures = {
            lane: executor.submit(
                _run_preflight_with_budget,
                machines[lane],
                budgets["preflight"],
                target=lane,
                proxy_required=proxy_required,
                proxy_port=proxy_port,
                require_android_proxy_match=False,
                device_serial=device_serials.get(lane, ""),
                wda_url=wda_url if lane == "ios" else "",
                auto_start_wda=auto_start_wda if lane == "ios" else False,
                allow_wda_reinstall=allow_wda_reinstall if lane == "ios" else False,
                wda_start_command=wda_start_command if lane == "ios" else "",
                wda_iproxy_command=wda_iproxy_command if lane == "ios" else "",
            )
            for lane in targets
        }
        preflights = {lane: future.result() for lane, future in futures.items()}
    failures = [
        {"code": "preflight_blocked", "target": lane, "blockers": result.as_dict().get("blockers", [])}
        for lane, result in preflights.items()
        if not result.ok
    ]
    if failures:
        return _persist_readiness_result(
            store,
            readiness_id,
            {"ok": False, "status": "readiness_failed", "failures": failures, "preflights": preflights},
        )

    drivers = {
        lane: _make_driver(target=lane, device_serial=device_serials.get(lane, ""), wda_url=wda_url)
        for lane in targets
    }
    if not proxy_required:
        return _persist_readiness_result(
            store,
            readiness_id,
            {
                "ok": True,
                "status": "ready",
                "preflights": preflights,
                "drivers": drivers,
                "manager": None,
                "lease": None,
                "proxy_host": "",
                "proxy_port": 0,
            },
        )

    expected_port = int(proxy_port or next(iter(preflights.values())).expected_proxy_port)
    instruction = build_proxy_instruction(targets[0], expected_port, proxy_required=True)
    candidates = instruction.get("phone_proxy_host_candidates") or []
    if not candidates:
        return _persist_readiness_result(
            store,
            readiness_id,
            {
                "ok": False,
                "status": "readiness_failed",
                "failures": [{"code": "proxy_host_unavailable", "message": "未发现可供手机路由访问的本机 IPv4 地址"}],
                "preflights": preflights,
            },
        )
    from mobile_auto_mcp.proxy.host_selection import (
        discover_device_wifi_ip,
        probe_proxy_host_reachability,
        select_proxy_host,
    )

    device_networks = {
        lane: discover_device_wifi_ip(lane, device_serials.get(lane, ""), drivers.get(lane))
        for lane in targets
    }
    device_wifi_ips = {
        lane: str(result.get("device_network") or result.get("device_ip") or "")
        for lane, result in device_networks.items()
    }
    selection = select_proxy_host(
        [str(candidate) for candidate in candidates],
        explicit_host=proxy_host,
        device_wifi_ips=device_wifi_ips,
    )
    if not selection.get("ok"):
        return _persist_readiness_result(
            store,
            readiness_id,
            {
                "ok": False,
                "status": "readiness_failed",
                "failures": [
                    {
                        "code": str(selection.get("code") or "proxy_host_unproven"),
                        "message": "无法证明电脑代理地址可从全部目标手机的当前 Wi-Fi 路由访问",
                        "selection": selection,
                        "device_networks": device_networks,
                    }
                ],
                "preflights": preflights,
                "device_networks": device_networks,
            },
        )
    proxy_host = str(selection["host"])
    route_proof = probe_proxy_host_reachability(proxy_host, device_wifi_ips)
    if not route_proof.get("ok"):
        return _persist_readiness_result(
            store,
            readiness_id,
            {
                "ok": False,
                "status": "readiness_failed",
                "failures": [
                    {
                        "code": "proxy_host_unreachable",
                        "message": "本机代理地址无法通过指定源网卡路由到全部目标手机",
                        "route_proof": route_proof,
                    }
                ],
                "preflights": preflights,
                "device_networks": device_networks,
            },
        )
    from mobile_auto_mcp.proxy.recovery import ProxyRecoveryManager

    recovery_manager = ProxyRecoveryManager(store.home)
    recovery_existed_before = recovery_manager.load_pending() is not None
    manager = ProxyManager(store.home, target=targets[0], port=expected_port)
    try:
        manager.start()
        adapters = [build_device_proxy_adapter(lane, device_serials.get(lane, ""), drivers[lane]) for lane in targets]
        lease = ManagedProxyLease(
            adapters,
            proxy_host,
            expected_port,
            event_sink=lambda event: store.proxy_state.record_event(readiness_id, {"readiness": True, **event}),
            snapshot_sink=lambda snapshots: _require_proxy_recovery_persisted(
                store,
                readiness_id,
                snapshots,
                manager,
            ),
        )
        acquired = lease.acquire()
        if not acquired.get("ok"):
            failure_stage = str((acquired.get("failure") or {}).get("stage") or "")
            rollback_ok = bool((acquired.get("rollback") or {}).get("ok"))
            if not recovery_existed_before and (failure_stage in {"snapshot", "snapshot_persist"} or rollback_ok):
                # No phone remains modified and no older retained run owns this record, so stale recovery is unsafe noise.
                recovery_manager.clear()
            manager.stop()
            return _persist_readiness_result(
                store,
                readiness_id,
                {
                    "ok": False,
                    "status": "readiness_failed",
                    "failures": [acquired.get("failure") or {"code": "device_proxy_acquire_failed"}],
                    "preflights": preflights,
                    "proxy_lifecycle": acquired,
                },
            )
    except Exception as exc:
        manager.stop()
        return _persist_readiness_result(
            store,
            readiness_id,
            {
                "ok": False,
                "status": "readiness_failed",
                "failures": [{"code": "managed_proxy_prepare_failed", "message": str(exc), "error_type": exc.__class__.__name__}],
                "preflights": preflights,
            },
        )
    return _persist_readiness_result(
        store,
        readiness_id,
        {
            "ok": True,
            "status": "ready",
            "preflights": preflights,
            "drivers": drivers,
            "manager": manager,
            "lease": lease,
            "proxy_host": proxy_host,
            "proxy_port": expected_port,
            "proxy_lifecycle": acquired,
            "proxy_host_selection": selection,
            "proxy_route_proof": route_proof,
            "device_networks": device_networks,
        },
    )


def _require_proxy_recovery_persisted(
    store: LocalStore,
    readiness_id: str,
    snapshots: dict[str, Any],
    manager: ProxyManager,
) -> dict[str, Any]:
    """Persist original proxy state before acquisition performs its first device write."""
    from mobile_auto_mcp.proxy.recovery import ProxyRecoveryManager

    serialized = {
        target: snapshot.__dict__ if hasattr(snapshot, "__dict__") else dict(snapshot)
        for target, snapshot in snapshots.items()
    }
    return ProxyRecoveryManager(store.home).persist(
        readiness_id,
        serialized,
        manager.runtime_evidence(),
    )


def _persist_readiness_result(store: LocalStore, readiness_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Write a JSON-safe readiness diagnostic outside the formal session registry."""
    payload = {"readiness_id": readiness_id, **result}
    serializable = {
        key: value
        for key, value in payload.items()
        if key not in {"drivers", "manager", "lease", "preflights"}
    }
    serializable["preflights"] = {
        lane: preflight.as_dict() if hasattr(preflight, "as_dict") else preflight
        for lane, preflight in (payload.get("preflights") or {}).items()
    }
    path = store.home / "readiness" / f"{readiness_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**payload, "readiness_report": str(path)}


def _readiness_failure_result(imported: dict[str, Any], readiness: dict[str, Any]) -> dict[str, Any]:
    """Return a failed preparation result without inventing a formal session or report."""
    return {
        "ok": False,
        "status": "readiness_failed",
        "session_id": "",
        "imported": imported,
        "runs": [],
        "report": {},
        "readiness": {
            key: value for key, value in readiness.items() if key not in {"drivers", "manager", "lease", "preflights"}
        },
        "preflight": {
            "ok": False,
            "lanes": {
                lane: value.as_dict() if hasattr(value, "as_dict") else value
                for lane, value in (readiness.get("preflights") or {}).items()
            },
        },
        "proxy_lifecycle": readiness.get("proxy_lifecycle") or {"status": "not_acquired", "auto_stop": True},
        "transport_contract": "single_mcp_call_internal_workers",
    }


def _retain_managed_environment(readiness: dict[str, Any], store: LocalStore, session_id: str) -> dict[str, Any]:
    """Retain verified phone and mitmproxy settings after a run and persist the required manual-close reminder."""
    if readiness.get("_finalize_result"):
        return dict(readiness["_finalize_result"])
    lease: ManagedProxyLease | None = readiness.get("lease")
    manager: ProxyManager | None = readiness.get("manager")
    acquire = readiness.get("proxy_lifecycle") or {"ok": True, "state": "not_required"}
    retained = bool(lease or manager)
    recovery_record: dict[str, Any] = {}
    recovery_failure: dict[str, Any] = {}
    if retained:
        from mobile_auto_mcp.proxy.recovery import ProxyRecoveryManager

        snapshots = {
            target: snapshot.__dict__ if hasattr(snapshot, "__dict__") else dict(snapshot)
            for target, snapshot in ((getattr(lease, "snapshots", {}) or {}).items() if lease else [])
        }
        runtime = manager.runtime_evidence() if manager else {}
        try:
            recovery_record = ProxyRecoveryManager(store.home).persist(session_id, snapshots, runtime)
        except Exception as exc:
            recovery_failure = {
                "code": "proxy_recovery_persist_failed",
                "message": str(exc),
                "error_type": exc.__class__.__name__,
            }
    lifecycle = {
        "status": "retention_failed" if recovery_failure else ("retained" if retained else "not_required"),
        "acquire": acquire,
        "release": {"ok": True, "state": "skipped_by_policy", "reason": "retain_for_manual_close"},
        "verified": bool(acquire.get("ok", True)) and not recovery_failure,
        "proxy_host": readiness.get("proxy_host") or "",
        "proxy_port": readiness.get("proxy_port") or 0,
        "mitmproxy_retained": bool(manager),
        "phone_proxy_retained": bool(lease),
        "auto_stop": False,
        "manual_cleanup_required": retained,
        "user_reminder": MANUAL_PROXY_REMINDER if retained else "",
        "phone_proxy_mutation_allowed": True,
        "phone_proxy_policy": "managed_snapshot_apply_verify_retain",
        "recovery_record": {
            "persisted": bool(recovery_record),
            "path": str(store.home / "proxy" / "pending_proxy_recovery.json") if recovery_record else "",
            "failure": recovery_failure,
        },
    }
    readiness["_finalize_result"] = lifecycle
    store.update_session_metadata(session_id, proxy_lifecycle=lifecycle)
    ExecutionEventStore(store.home).append(
        {
            "event": "managed_proxy_retained",
            "session_id": session_id,
            "lane_id": "coordinator",
            "stage": "finalize",
            "ok": lifecycle["verified"],
            "proxy_lifecycle": lifecycle,
        }
    )
    store.proxy_state.record_event(session_id, {"event": "managed_proxy_retained", "proxy_lifecycle": lifecycle})
    return lifecycle


def _resolve_stage_budgets(overrides: dict[str, float] | None, probe_timeout_seconds: float) -> dict[str, float]:
    """Resolve stage budgets using the supplied state and inputs."""
    budgets = {**DEFAULT_STAGE_BUDGETS, "target_api_probe": float(probe_timeout_seconds), "await_hit": float(probe_timeout_seconds)}
    for stage, value in (overrides or {}).items():
        if stage in budgets and float(value) > 0:
            budgets[stage] = float(value)
    return budgets


def _run_preflight_with_budget(
    machine: LaneStateMachine,
    budget_seconds: float,
    **kwargs: Any,
) -> PreflightResult:
    """Run preflight with budget using the supplied state and inputs."""
    try:
        return machine.run(
            "preflight",
            budget_seconds,
            lambda: run_preflight(**kwargs),
            failure_code="preflight_blocked",
        )
    except LaneStageTimeout as exc:
        target = str(kwargs.get("target") or machine.lane_id)
        expected_port = int(kwargs.get("proxy_port") or 13000)
        failure = build_failure("stage_timeout", target, "preflight", evidence=exc.as_dict())
        return PreflightResult(
            ok=False,
            target=target,
            proxy_required=bool(kwargs.get("proxy_required", True)),
            expected_proxy_port=expected_port,
            checks={"stage_timeout": exc.as_dict()},
            blockers=[str(exc)],
            warnings=[],
            failures=[failure],
            phone_proxy_hint="前置检查超时，仅重试当前设备端。",
            proxy_instruction={},
        )


def _stage_timeout_result(platform: str, error: LaneStageTimeout) -> dict[str, Any]:
    """Handle stage timeout result using the supplied state and inputs."""
    failure = build_failure("stage_timeout", platform, error.stage, evidence=error.as_dict())
    return {
        "ok": False,
        "stage": error.stage,
        "status": "stage_timeout",
        "message": str(error),
        "failure": failure,
        "timed_out": True,
        **error.as_dict(),
    }


def _remaining_rule_budget(
    deadline: float | None,
    total_budget: float,
    stage_budget: float,
    stage: str,
) -> float:
    """Handle remaining rule budget using the supplied state and inputs."""
    if deadline is None:
        return float(stage_budget)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LaneStageTimeout(stage, float(total_budget), float(total_budget))
    return min(float(stage_budget), remaining)


def run_cases(
    *,
    case_file: str = "",
    knowledge_dir: str = "",
    app_id: str,
    target: str = "android",
    target_page: str = "",
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
    base_home: str = "",
    rule_keyword: str = "",
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
    stop_on_no_request: bool = True,
    probe_timeout_seconds: float = 12,
    stage_budgets: dict[str, float] | None = None,
    auto_visual_compare: bool = True,
) -> dict[str, Any]:
    """Run cases end to end. app_id is storage namespace only; package launches the app."""
    from mobile_auto_mcp.cases.parser import import_case_file

    home = workspace_home(base_home=base_home or None, tenant_id=tenant_id, workspace_id=workspace_id, app_id=app_id)
    # 知识库是工作流默认能力；调用方可覆盖目录，但无需了解或显式传参。
    knowledge_dir = knowledge_dir or str(home / "knowledge")
    store = LocalStore(home)
    imported = import_case_file(store, case_file, knowledge_dir=knowledge_dir) if case_file else {"saved": []}
    rules = _select_rules(store.list_rules(keyword=rule_keyword, enabled_only=True), rule_ids)
    execution_contract = validate_execution_contract(
        rules,
        requested_rule_ids=rule_ids,
        target_page=target_page,
        target_page_assertions=target_page_assertions,
    )
    if not execution_contract.get("ok"):
        return {
            "ok": False,
            "status": "invalid_execution_contract",
            "session_id": "",
            "imported": imported,
            "runs": [],
            "report": {},
            "execution_contract": execution_contract,
            "transport_contract": "single_mcp_call_internal_workers",
        }
    normalized_target = target.lower()
    if normalized_target in {"both", "dual", "triple", "all", "三端"}:
        targets = ["android", "ios", "harmony"] if normalized_target in {"triple", "all", "三端"} else ["android", "ios"]
        encoded_packages = _parse_target_package_mapping(target_app_package)
        encoded_navigation = _split_navigation_path_by_lane(navigation_path or [])
        package_map = target_app_packages or encoded_packages
        knowledge_navigation = {
            lane: _suggest_navigation_path(
                knowledge_dir=knowledge_dir,
                app_id=app_id,
                target_page=target_page,
                target=lane,
                context=navigation_context,
            )
            for lane in targets
        }
        knowledge_navigation = {lane: path for lane, path in knowledge_navigation.items() if path}
        navigation_map = navigation_paths or encoded_navigation or knowledge_navigation
        knowledge_triggers = {
            lane: _suggest_request_trigger_path(
                knowledge_dir=knowledge_dir,
                app_id=app_id,
                target_page=target_page,
                target=lane,
                context=navigation_context,
            )
            for lane in targets
        }
        trigger_map = request_trigger_paths or {lane: path for lane, path in knowledge_triggers.items() if path}
        serial_map = _target_device_serials(
            targets,
            device_serial=device_serial,
            android_serial=android_serial,
            ios_udid=ios_udid,
            harmony_serial=harmony_serial,
        )
        fallback_navigation_path = navigation_path or [] if not encoded_navigation else []
        navigation_path_source = "provided_by_lane" if navigation_paths or encoded_navigation else ("knowledge_by_lane" if knowledge_navigation else ("provided" if navigation_path else "none"))
        return _run_dual_cases(
            targets=targets,
            store=store,
            imported=imported,
            rules=rules,
            target_app_packages=package_map,
            fallback_target_app_package="" if package_map else target_app_package,
            navigation_paths=navigation_map,
            fallback_navigation_path=fallback_navigation_path,
            request_trigger_paths=trigger_map,
            fallback_request_trigger_path=request_trigger_path or [],
            navigation_path_source=navigation_path_source,
            navigation_context=navigation_context,
            target_page=target_page,
            target_page_assertions=target_page_assertions,
            knowledge_dir=knowledge_dir,
            app_id=app_id,
            proxy_required=proxy_required,
            proxy_port=proxy_port,
            proxy_host=proxy_host,
            device_serials=serial_map,
            wda_url=wda_url,
            auto_start_wda=auto_start_wda,
            allow_wda_reinstall=allow_wda_reinstall,
            wda_start_command=wda_start_command,
            wda_iproxy_command=wda_iproxy_command,
            stop_on_no_request=stop_on_no_request,
            probe_timeout_seconds=probe_timeout_seconds,
            stage_budgets=stage_budgets,
            auto_visual_compare=auto_visual_compare,
        )
    suggested_navigation_path = _suggest_navigation_path(
        knowledge_dir=knowledge_dir,
        app_id=app_id,
        target_page=target_page,
        target=normalized_target,
        context=navigation_context,
        allow_legacy=True,
    )
    effective_navigation_path = navigation_path or suggested_navigation_path
    effective_request_trigger_path = _resolve_request_trigger_path(
        knowledge_dir=knowledge_dir,
        app_id=app_id,
        target_page=target_page,
        target=normalized_target,
        context=navigation_context,
        explicit_path=request_trigger_path,
    )
    navigation_path_source = "provided" if navigation_path else ("knowledge" if suggested_navigation_path else "none")
    single_serial = _target_device_serials(
        [normalized_target],
        device_serial=device_serial,
        android_serial=android_serial,
        ios_udid=ios_udid,
        harmony_serial=harmony_serial,
    )[normalized_target]
    budgets = _resolve_stage_budgets(stage_budgets, probe_timeout_seconds)
    readiness = prepare_managed_environment(
        store=store,
        targets=[normalized_target],
        device_serials={normalized_target: single_serial},
        proxy_required=proxy_required,
        proxy_port=proxy_port,
        proxy_host=proxy_host,
        wda_url=wda_url,
        auto_start_wda=auto_start_wda,
        allow_wda_reinstall=allow_wda_reinstall,
        wda_start_command=wda_start_command,
        wda_iproxy_command=wda_iproxy_command,
        stage_budgets=stage_budgets,
    )
    if not readiness.get("ok"):
        return _readiness_failure_result(imported, readiness)
    session = store.start_session(target=target, rule_ids=[rule["id"] for rule in rules])
    session_id = session["session_id"]
    event_store = ExecutionEventStore(store.home)
    machine = LaneStateMachine(event_store, session_id, normalized_target)
    preflight = readiness["preflights"][normalized_target]
    if skip_preflight:
        preflight.warnings.append("skip_preflight 已请求，但 Android WLAN 代理端口一致性仍是硬门禁，不能绕过")
    driver = (readiness.get("drivers") or {}).get(normalized_target) or _make_driver(
        target=target, device_serial=single_serial, wda_url=wda_url
    )
    results: list[dict[str, Any]] = []
    try:
        try:
            nav = machine.run(
                "navigation",
                budgets["navigation"],
                lambda: _navigate(driver, target_app_package, effective_navigation_path or [], target_page, target_page_assertions),
                failure_code="navigation_failed",
            )
        except LaneStageTimeout as exc:
            nav = _stage_timeout_result(normalized_target, exc)
        if not nav.get("ok"):
            navigation_persistence = _persist_navigation_path(
                knowledge_dir=knowledge_dir,
                app_id=app_id,
                target_page=target_page,
                path=effective_navigation_path or [],
                navigation_ok=False,
                request_hit=False,
                source=navigation_path_source,
                target=normalized_target,
                context=navigation_context,
            )
            for rule in rules:
                results.append(
                    store.record_run_result(
                        session_id,
                        target,
                        rule["id"],
                        status="invalid_execution",
                        review_note="进入目标页面失败，未执行异常规则",
                        execution_gate={"stage": "navigation", "navigation": nav},
                    )
                )
            store.update_session_status(session_id, "blocked")
            lifecycle = _retain_managed_environment(readiness, store, session_id)
            report = export_archive_report(store, session_id, report_dir=store.home / "reports" / session_id)
            return _result(store, session_id, imported, preflight.as_dict(), results, report, status="blocked", navigation=nav, navigation_persistence=navigation_persistence, navigation_path_source=navigation_path_source, proxy_lifecycle=lifecycle)
        try:
            probe = machine.run(
                "target_api_probe",
                budgets["target_api_probe"],
                lambda: _target_api_probe(store, session_id, target, rules, driver, budgets["target_api_probe"], effective_request_trigger_path),
                failure_code="target_api_not_observed",
            )
        except LaneStageTimeout as exc:
            probe = _stage_timeout_result(normalized_target, exc)
        navigation_persistence = _persist_navigation_path(
            knowledge_dir=knowledge_dir,
            app_id=app_id,
            target_page=target_page,
            path=effective_navigation_path or [],
            navigation_ok=bool(nav.get("ok")),
            request_hit=bool(probe.get("ok")),
            source=navigation_path_source,
            target=normalized_target,
            context=navigation_context,
        )
        if not probe.get("ok"):
            for rule in rules:
                results.append(
                    store.record_run_result(
                        session_id,
                        target,
                        rule["id"],
                        status="invalid_execution",
                        review_note=probe.get("message", "目标接口探针失败"),
                        execution_gate={"stage": "target_api_probe", "probe": probe},
                    )
                )
            store.update_session_status(session_id, "blocked")
            lifecycle = _retain_managed_environment(readiness, store, session_id)
            report = export_archive_report(store, session_id, report_dir=store.home / "reports" / session_id)
            return _result(store, session_id, imported, preflight.as_dict(), results, report, status="blocked", navigation=nav, probe=probe, navigation_persistence=navigation_persistence, navigation_path_source=navigation_path_source, proxy_lifecycle=lifecycle)
        for rule in rules:
            try:
                result = _execute_rule(
                    store,
                    session_id,
                    target,
                    rule,
                    driver,
                    stop_on_no_request,
                    target_page=target_page,
                    target_page_assertions=target_page_assertions,
                    request_trigger_path=effective_request_trigger_path,
                )
            except Exception as exc:
                # A device or screenshot exception must become an auditable partial result, not an abandoned Session.
                result = store.record_run_result(
                    session_id,
                    target,
                    rule["id"],
                    status="invalid_execution",
                    review_note=str(exc),
                    execution_gate={
                        "stage": "rule_exception",
                        "code": "rule_execution_exception",
                        "error_type": exc.__class__.__name__,
                    },
                )
            results.append(result)
        execution_status = derive_single_run_status(results)
        visual_precheck = (
            apply_session_visual_comparison(store, session_id, expected_targets=[normalized_target])
            if auto_visual_compare
            else {"engine": "disabled", "cases": []}
        )
        results = store.list_runs(session_id)
        store.update_session_status(session_id, str(execution_status["status"]))
        lifecycle = _retain_managed_environment(readiness, store, session_id)
        report = export_archive_report(store, session_id, report_dir=store.home / "reports" / session_id)
        return _result(
            store,
            session_id,
            imported,
            preflight.as_dict(),
            results,
            report,
            status=str(execution_status["status"]),
            execution_ok=bool(execution_status.get("execution_ok")),
            execution_status=execution_status,
            visual_review_mode="precheck_then_semantic_required" if auto_visual_compare else "semantic_required",
            visual_precheck=visual_precheck,
            navigation=nav,
            probe=probe,
            navigation_persistence=navigation_persistence,
            navigation_path_source=navigation_path_source,
            proxy_lifecycle=lifecycle,
            transport_contract="single_mcp_call_internal_workers",
        )
    finally:
        store.proxy_state.clear_active()
        store.proxy_state.clear_probe()
        _retain_managed_environment(readiness, store, session_id)


def _navigate(
    driver: DeviceDriver,
    target_app_package: str,
    navigation_path: list[dict[str, Any]],
    target_page: str,
    target_page_assertions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Handle navigate using the supplied state and inputs."""
    steps: list[dict[str, Any]] = []
    if target_app_package:
        steps.append(driver.launch_app(target_app_package))
    if navigation_path:
        result = driver.navigate_path(navigation_path)
        steps.extend(result.get("steps") or [result])
    elif target_page:
        steps.append({"ok": True, "action": "target_page_hint", "target_page": target_page, "message": "未提供 navigation_path，将仅通过目标页断言确认"})
    verification = _verify_target_page(driver, target_page, target_page_assertions) if target_page or target_page_assertions else {"ok": True, "verified": True}
    if target_page:
        steps.append({"ok": verification.get("ok", False), "action": "assert_target_page", "target_page": target_page, "verification": verification, "verified": verification.get("ok", False)})
    return {
        "ok": bool(steps) and all(step.get("ok", False) for step in steps) and bool(verification.get("ok", True)),
        "verified": bool(verification.get("ok", True)),
        "verification": verification,
        "steps": steps,
    }


def _verify_target_page(driver: DeviceDriver, target_page: str, target_page_assertions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Verify target page using the supplied state and inputs."""
    assertions = target_page_assertions or _target_page_assertions(target_page)
    if not assertions:
        return {"ok": False, "verified": False, "message": "缺少 target_page，无法验证页面"}
    if not hasattr(driver, "assert_page"):
        return {"ok": False, "verified": False, "message": "当前设备驱动不支持页面断言", "assertions": assertions}
    result = driver.assert_page(assertions)
    return {"assertions": assertions, **result}


def _target_page_assertions(target_page: str) -> list[dict[str, Any]]:
    """Handle target page assertions using the supplied state and inputs."""
    text = (target_page or "").strip()
    if not text:
        return []
    tokens = [token.strip() for token in re.split(r"[-_/>\s]+", text) if token.strip()]
    last = tokens[-1] if tokens else text
    aliases = []
    for candidate in (text, last):
        if candidate and candidate not in aliases:
            aliases.append(candidate)
    return [{"any_text": aliases}]


def _persist_navigation_path(
    *,
    knowledge_dir: str,
    app_id: str,
    target_page: str,
    path: list[dict[str, Any]],
    navigation_ok: bool,
    request_hit: bool,
    targets: list[str] | None = None,
    source: str = "",
    target: str = "",
    context: str = "",
) -> dict[str, Any]:
    """在目标页验证通过后，按端和上下文持久化一条可复用路径。"""
    if not knowledge_dir:
        return {"status": "skipped", "reason": "knowledge_dir_not_provided"}
    if not app_id:
        return {"status": "skipped", "reason": "app_id_not_provided", "knowledge_dir": knowledge_dir}
    if not target_page:
        return {"status": "skipped", "reason": "target_page_not_provided", "knowledge_dir": knowledge_dir, "app_id": app_id}
    if not navigation_ok:
        return {"status": "skipped", "reason": "navigation_not_verified", "knowledge_dir": knowledge_dir, "app_id": app_id, "target_page": target_page}
    if not request_hit:
        return {"status": "skipped", "reason": "target_request_not_verified", "knowledge_dir": knowledge_dir, "app_id": app_id, "target_page": target_page}
    if not path:
        return {"status": "skipped", "reason": "navigation_path_not_provided", "knowledge_dir": knowledge_dir, "app_id": app_id, "target_page": target_page}
    knowledge = KnowledgeBase(knowledge_dir)
    recorded = knowledge.record_navigation_path(app_id, target_page, path, target=target, context=context)
    readback = knowledge.suggest_navigation_path(app_id, target_page, target=target, context=context)
    verified_by_readback = readback.get("path") == path
    return {
        **recorded,
        "knowledge_dir": knowledge_dir,
        "targets": targets or [],
        "source": source,
        "verified_by_readback": verified_by_readback,
        "readback": readback,
    }


def _persist_navigation_paths(
    *,
    knowledge_dir: str,
    app_id: str,
    target_page: str,
    lanes: list[dict[str, Any]],
    navigation: dict[str, dict[str, Any]],
    probes: dict[str, dict[str, Any]],
    source: str,
    context: str,
) -> dict[str, Any]:
    """分别保存各端已验证路径，禁止把任一端路径当作其他端的默认路径。"""
    records = {
        lane["lane_id"]: _persist_navigation_path(
            knowledge_dir=knowledge_dir,
            app_id=app_id,
            target_page=target_page,
            path=lane.get("navigation_path") or [],
            navigation_ok=bool((navigation.get(lane["lane_id"]) or {}).get("ok")),
            request_hit=bool((probes.get(lane["lane_id"]) or {}).get("ok")),
            targets=[lane["lane_id"]],
            source=source,
            target=lane["lane_id"],
            context=context,
        )
        for lane in lanes
    }
    statuses = {record.get("status") for record in records.values()}
    canonical = _canonical_navigation_path(lanes)
    return {
        "status": "saved" if "saved" in statuses else "skipped",
        "reason": "all_lane_paths_skipped" if statuses == {"skipped"} else "",
        "path": canonical,
        "paths": {lane["lane_id"]: lane.get("navigation_path") or [] for lane in lanes},
        "targets": [lane["lane_id"] for lane in lanes],
        "source": source,
        "records": records,
    }


def _persist_request_trigger_paths(
    *,
    knowledge_dir: str,
    app_id: str,
    target_page: str,
    lanes: list[dict[str, Any]],
    navigation: dict[str, dict[str, Any]],
    probes: dict[str, dict[str, Any]],
    context: str,
) -> dict[str, Any]:
    """仅把页面和目标请求都验证成功的触发路径写回知识库。"""
    if not knowledge_dir or not app_id or not target_page:
        return {"status": "skipped", "reason": "missing_knowledge_scope"}
    knowledge = KnowledgeBase(knowledge_dir)
    records: dict[str, Any] = {}
    for lane in lanes:
        lane_id = lane["lane_id"]
        path = lane.get("request_trigger_path") or []
        records[lane_id] = knowledge.record_request_trigger_path(
            app_id,
            target_page,
            path,
            target=lane.get("target", ""),
            context=context,
            request_hit=bool((probes.get(lane_id) or {}).get("ok")),
            page_verified=bool((navigation.get(lane_id) or {}).get("ok")),
        )
    statuses = {record.get("status") for record in records.values()}
    return {"status": "saved" if "saved" in statuses else "skipped", "records": records}


def _suggest_navigation_path(
    *,
    knowledge_dir: str,
    app_id: str,
    target_page: str,
    target: str = "",
    context: str = "",
    allow_legacy: bool = False,
) -> list[dict[str, Any]]:
    """读取精确作用域路径；仅单端兼容模式允许回退旧版无作用域知识。"""
    if not knowledge_dir or not app_id or not target_page:
        return []
    knowledge = KnowledgeBase(knowledge_dir)
    scoped = knowledge.suggest_navigation_path(app_id, target_page, target=target, context=context).get("path") or []
    if scoped or not allow_legacy:
        return scoped
    return knowledge.suggest_navigation_path(app_id, target_page).get("path") or []


def _suggest_request_trigger_path(
    *,
    knowledge_dir: str,
    app_id: str,
    target_page: str,
    target: str,
    context: str,
) -> list[dict[str, Any]]:
    """读取经过验证的请求触发路径，不跨端或跨页面猜测。"""
    if not knowledge_dir or not app_id or not target_page:
        return []
    knowledge = KnowledgeBase(knowledge_dir)
    return knowledge.suggest_request_trigger_path(
        app_id,
        target_page,
        target=target,
        context=context,
    ).get("path") or []


def _resolve_request_trigger_path(
    *,
    knowledge_dir: str,
    app_id: str,
    target_page: str,
    target: str,
    context: str,
    explicit_path: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """优先使用调用方路径，否则复用同一作用域的知识库路径。"""
    return explicit_path or _suggest_request_trigger_path(
        knowledge_dir=knowledge_dir,
        app_id=app_id,
        target_page=target_page,
        target=target,
        context=context,
    )


def _canonical_navigation_path(lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Handle canonical navigation path using the supplied state and inputs."""
    for lane in lanes:
        path = lane.get("navigation_path") or []
        if path:
            return path
    return []


def _parse_target_package_mapping(value: str) -> dict[str, str]:
    """Parse target package mapping using the supplied state and inputs."""
    text = (value or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return {str(k): str(v) for k, v in payload.items() if v}
    if "android=" in text or "ios=" in text or "harmony=" in text:
        mapping: dict[str, str] = {}
        for item in text.split(","):
            if "=" not in item:
                continue
            key, package = item.split("=", 1)
            key = key.strip()
            if key in {"android", "ios", "harmony"} and package.strip():
                mapping[key] = package.strip()
        return mapping
    return {}


def _split_navigation_path_by_lane(path: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Split navigation path by lane using the supplied state and inputs."""
    lanes: dict[str, list[dict[str, Any]]] = {}
    for step in path:
        lane_id = str(step.get("lane_id") or step.get("target") or "")
        if lane_id not in {"android", "ios", "harmony"}:
            continue
        copied = {k: v for k, v in step.items() if k not in {"lane_id", "target"}}
        if "action" not in copied:
            copied["action"] = "click"
        lanes.setdefault(lane_id, []).append(copied)
    return lanes


def _select_rules(rules: list[dict[str, Any]], rule_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Return exact rules in caller order when IDs are supplied."""
    if not rule_ids:
        return rules
    by_id = {str(rule.get("id") or ""): rule for rule in rules}
    return [by_id[rule_id] for rule_id in rule_ids if rule_id in by_id]


def _target_device_serials(
    targets: list[str],
    *,
    device_serial: str = "",
    android_serial: str = "",
    ios_udid: str = "",
    harmony_serial: str = "",
) -> dict[str, str]:
    """Resolve a device ID per target without leaking one lane's ID into another."""
    explicit = {"android": android_serial, "ios": ios_udid, "harmony": harmony_serial}
    single_target = len(targets) == 1
    return {
        target: explicit.get(target, "") or (device_serial if single_target else "")
        for target in targets
    }


def _trigger_request(driver: DeviceDriver, path: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Execute only an explicit request trigger; never infer a page action."""
    if not path:
        return {
            "ok": False,
            "stage": "request_trigger",
            "status": "request_trigger_missing",
            "message": "目标接口需要显式 request_trigger_path，或先沉淀经过验证的请求触发路径",
        }
    result = driver.navigate_path(path)
    return {"stage": "request_trigger", "status": "triggered" if result.get("ok") else "request_trigger_failed", **result}


def _run_dual_cases(
    *,
    targets: list[str],
    store: LocalStore,
    imported: dict[str, Any],
    rules: list[dict[str, Any]],
    target_app_packages: dict[str, str],
    fallback_target_app_package: str,
    navigation_paths: dict[str, list[dict[str, Any]]],
    fallback_navigation_path: list[dict[str, Any]],
    request_trigger_paths: dict[str, list[dict[str, Any]]],
    fallback_request_trigger_path: list[dict[str, Any]],
    navigation_path_source: str,
    navigation_context: str,
    target_page: str,
    target_page_assertions: list[dict[str, Any]] | None,
    knowledge_dir: str,
    app_id: str,
    proxy_required: bool,
    proxy_port: int | None,
    proxy_host: str,
    device_serials: dict[str, str],
    wda_url: str,
    auto_start_wda: bool,
    allow_wda_reinstall: bool,
    wda_start_command: str,
    wda_iproxy_command: str,
    stop_on_no_request: bool,
    probe_timeout_seconds: float,
    stage_budgets: dict[str, float] | None,
    auto_visual_compare: bool,
) -> dict[str, Any]:
    """Prepare all requested devices before creating one formal multi-lane execution session."""
    shared_port = int(proxy_port or 13000)
    budgets = _resolve_stage_budgets(stage_budgets, probe_timeout_seconds)
    readiness = prepare_managed_environment(
        store=store,
        targets=targets,
        device_serials=device_serials,
        proxy_required=proxy_required,
        proxy_port=shared_port,
        proxy_host=proxy_host,
        wda_url=wda_url,
        auto_start_wda=auto_start_wda,
        allow_wda_reinstall=allow_wda_reinstall,
        wda_start_command=wda_start_command,
        wda_iproxy_command=wda_iproxy_command,
        stage_budgets=stage_budgets,
    )
    if not readiness.get("ok"):
        return _readiness_failure_result(imported, readiness)
    session = store.start_session(target="all", rule_ids=[rule["id"] for rule in rules])
    session_id = session["session_id"]
    event_store = ExecutionEventStore(store.home)
    preflights = readiness["preflights"]
    failed_preflights: dict[str, Any] = {}
    viable_targets = list(targets)
    lanes = [
        {
            "lane_id": lane,
            "target": lane,
            "target_app_package": target_app_packages.get(lane) or (fallback_target_app_package if lane == "android" else ""),
            "navigation_path": navigation_paths.get(lane) or fallback_navigation_path,
            "request_trigger_path": request_trigger_paths.get(lane) or fallback_request_trigger_path,
            "device_serial": device_serials.get(lane, ""),
            "client_ip": "",
        }
        for lane in targets
    ]
    drivers = readiness.get("drivers") or {
        lane["lane_id"]: _make_driver(target=lane["target"], device_serial=lane["device_serial"], wda_url=wda_url)
        for lane in lanes
    }
    results: list[dict[str, Any]] = _record_lane_failures(
        store,
        session_id,
        "all",
        rules,
        failed_preflights,
        stage="preflight",
        note="设备前置检查失败",
    )
    rule_gates: list[dict[str, Any]] = []
    try:
        probe_coordinator = _LaneProbeCoordinator(store, session_id, _trigger_request)
        case_coordinator = _CaseCoordinator(session_id, rules, lanes, event_store)
        case_coordinator.submit_many(results)
        worker_results: dict[str, dict[str, Any]] = {}
        runnable_lanes = [lane for lane in lanes if lane["lane_id"] in viable_targets]
        probe_coordinator.prepare(runnable_lanes, rules)
        with ThreadPoolExecutor(max_workers=len(runnable_lanes)) as executor:
            futures = {
                executor.submit(
                    _run_lane_worker,
                    store,
                    session_id,
                    lane,
                    rules,
                    drivers[lane["lane_id"]],
                    probe_coordinator,
                    stop_on_no_request,
                    probe_timeout_seconds,
                    target_page,
                    target_page_assertions,
                    event_store,
                    case_coordinator,
                    budgets,
                ): lane["lane_id"]
                for lane in runnable_lanes
            }
            for future in as_completed(futures):
                lane_id = futures[future]
                try:
                    worker_results[lane_id] = future.result()
                except Exception as exc:
                    structured_failure = build_failure(
                        "lane_worker_exception",
                        lane_id,
                        "lane_worker",
                        evidence={"error": str(exc), "error_type": exc.__class__.__name__},
                    )
                    failure = {
                        "ok": False,
                        "lane_id": lane_id,
                        "message": str(exc) or exc.__class__.__name__,
                        "error_type": exc.__class__.__name__,
                        "failure": structured_failure,
                    }
                    failed_runs = _record_lane_failures(
                        store,
                        session_id,
                        lane_id,
                        rules,
                        {lane_id: failure},
                        stage="lane_worker",
                        note="设备 Worker 执行异常",
                    )
                    case_coordinator.submit_many(failed_runs)
                    worker_results[lane_id] = {"lane_id": lane_id, "navigation": failure, "probe": {}, "runs": failed_runs}
                results.extend(worker_results[lane_id]["runs"])

        navigation = {
            lane["lane_id"]: (
                worker_results.get(lane["lane_id"], {}).get("navigation")
                or {"ok": False, "message": "设备前置检查失败", "preflight": failed_preflights.get(lane["lane_id"], {})}
            )
            for lane in lanes
        }
        probes = {lane_id: result.get("probe") or {} for lane_id, result in worker_results.items()}
        navigation_persistence = _persist_navigation_paths(
            knowledge_dir=knowledge_dir,
            app_id=app_id,
            target_page=target_page,
            lanes=lanes,
            navigation=navigation,
            probes=probes,
            source=navigation_path_source,
            context=navigation_context,
        )
        trigger_persistence = _persist_request_trigger_paths(
            knowledge_dir=knowledge_dir,
            app_id=app_id,
            target_page=target_page,
            lanes=lanes,
            navigation=navigation,
            probes=probes,
            context=navigation_context,
        )
        lane_order = {lane_id: index for index, lane_id in enumerate(targets)}
        rule_order = {rule["id"]: index for index, rule in enumerate(rules)}
        results.sort(
            key=lambda run: (
                rule_order.get(str(run.get("rule_id") or (run.get("traceability") or {}).get("rule_id") or ""), len(rule_order)),
                lane_order.get(str((run.get("traceability") or {}).get("lane_id") or run.get("target") or ""), len(lane_order)),
            )
        )
        rule_gates = case_coordinator.gates()
        visual_comparison = (
            apply_session_visual_comparison(store, session_id, expected_targets=targets)
            if auto_visual_compare
            else {"engine": "disabled", "cases": []}
        )
        results = store.list_runs(session_id)
        results.sort(
            key=lambda run: (
                rule_order.get(str(run.get("rule_id") or (run.get("traceability") or {}).get("rule_id") or ""), len(rule_order)),
                lane_order.get(str((run.get("traceability") or {}).get("lane_id") or run.get("target") or ""), len(lane_order)),
            )
        )
        gates_passed = bool(rule_gates) and all(gate["passed"] for gate in rule_gates)
        aggregation = case_coordinator.snapshot()
        all_cases_terminal = aggregation["sealed_count"] == aggregation["total_cases"]
        execution_ok = gates_passed and all_cases_terminal
        final_status = "awaiting_review" if execution_ok else "partial"
        store.update_session_status(session_id, final_status)
        lifecycle = _retain_managed_environment(readiness, store, session_id)
        report = export_archive_report(store, session_id, report_dir=store.home / "reports" / session_id)
        return _result(
            store,
            session_id,
            imported,
            {"ok": True, "shared_proxy_port": shared_port, "lanes": {k: v.as_dict() for k, v in preflights.items()}},
            results,
            report,
            status=final_status,
            navigation=navigation,
            probe=probes,
            rule_gates=rule_gates,
            navigation_persistence=navigation_persistence,
            trigger_persistence=trigger_persistence,
            navigation_path_source=navigation_path_source,
            execution_mode="independent_lane_workers_case_aggregation",
            case_aggregation=aggregation,
            execution_ok=execution_ok,
            visual_review_mode="precheck_then_semantic_required" if auto_visual_compare else "semantic_required",
            visual_precheck=visual_comparison,
            stage_budgets=budgets,
            proxy_lifecycle=lifecycle,
            transport_contract="single_mcp_call_internal_workers",
        )
    finally:
        store.proxy_state.clear_active()
        store.proxy_state.clear_probe()
        _retain_managed_environment(readiness, store, session_id)


class _CaseCoordinator:
    """Seal case-level gates as independent lane workers reach terminal results."""

    def __init__(
        self,
        session_id: str,
        rules: list[dict[str, Any]],
        lanes: list[dict[str, Any]],
        events: ExecutionEventStore,
    ) -> None:
        """Initialize _CaseCoordinator state, configuration, and runtime dependencies."""
        self.session_id = session_id
        self.rules = {str(rule.get("id") or ""): rule for rule in rules}
        self.rule_order = [str(rule.get("id") or "") for rule in rules]
        self.lanes = lanes
        self.lane_order = [str(lane.get("lane_id") or "") for lane in lanes]
        self.events = events
        self._lock = threading.RLock()
        self._runs: dict[str, dict[str, dict[str, Any]]] = {rule_id: {} for rule_id in self.rule_order}
        self._gates: dict[str, dict[str, Any]] = {}

    def submit_many(self, runs: list[dict[str, Any]]) -> None:
        """Handle submit many using the supplied state and inputs."""
        for run in runs:
            self.submit(run)

    def submit(self, run: dict[str, Any]) -> dict[str, Any]:
        """Handle submit using the supplied state and inputs."""
        traceability = run.get("traceability") or {}
        rule_id = str(run.get("rule_id") or traceability.get("rule_id") or "")
        lane_id = str(traceability.get("lane_id") or run.get("target") or "")
        with self._lock:
            if rule_id not in self._runs or lane_id not in self.lane_order:
                return {"accepted": False, "sealed": False, "reason": "unknown_case_or_lane"}
            if lane_id in self._runs[rule_id]:
                return {"accepted": False, "sealed": rule_id in self._gates, "duplicate": True}
            self._runs[rule_id][lane_id] = run
            terminal_lanes = [lane for lane in self.lane_order if lane in self._runs[rule_id]]
            self.events.append(
                {
                    "event": "case_progress",
                    "session_id": self.session_id,
                    "lane_id": lane_id,
                    "stage": "case_aggregation",
                    "rule_id": rule_id,
                    "terminal_lanes": terminal_lanes,
                    "expected_lanes": self.lane_order,
                }
            )
            if len(terminal_lanes) != len(self.lane_order):
                return {"accepted": True, "sealed": False, "terminal_lanes": terminal_lanes}
            ordered_runs = [self._runs[rule_id][lane] for lane in self.lane_order]
            gate = _rule_count_gate(self.rules[rule_id], self.lanes, ordered_runs)
            self._gates[rule_id] = gate
            self.events.append(
                {
                    "event": "case_sealed",
                    "session_id": self.session_id,
                    "lane_id": "coordinator",
                    "stage": "case_aggregation",
                    "rule_id": rule_id,
                    "passed": bool(gate.get("passed")),
                    "terminal_lanes": terminal_lanes,
                }
            )
            return {"accepted": True, "sealed": True, "terminal_lanes": terminal_lanes, "gate": gate}

    def gates(self) -> list[dict[str, Any]]:
        """Handle gates using the supplied state and inputs."""
        with self._lock:
            return [self._gates[rule_id] for rule_id in self.rule_order if rule_id in self._gates]

    def snapshot(self) -> dict[str, Any]:
        """Handle snapshot using the supplied state and inputs."""
        with self._lock:
            cases = {
                rule_id: {
                    "sealed": rule_id in self._gates,
                    "terminal_lanes": [lane for lane in self.lane_order if lane in self._runs[rule_id]],
                    "pending_lanes": [lane for lane in self.lane_order if lane not in self._runs[rule_id]],
                    "gate": self._gates.get(rule_id, {}),
                }
                for rule_id in self.rule_order
            }
        return {
            "contract": "independent_workers_async_case_sealing",
            "expected_lanes": list(self.lane_order),
            "sealed_count": sum(1 for case in cases.values() if case["sealed"]),
            "total_cases": len(cases),
            "cases": cases,
        }


class _LaneProbeCoordinator(LaneProbeCoordinator):
    """Compatibility wrapper that supplies the runner's request-trigger implementation."""

    def __init__(self, store: LocalStore, session_id: str, trigger_request: Any = None) -> None:
        """Initialize the extracted coordinator without changing the legacy constructor."""
        super().__init__(store, session_id, trigger_request or _trigger_request)


def _run_lane_worker(
    store: LocalStore,
    session_id: str,
    lane: dict[str, Any],
    rules: list[dict[str, Any]],
    driver: DeviceDriver,
    probe_coordinator: _LaneProbeCoordinator,
    stop_on_no_request: bool,
    probe_timeout_seconds: float,
    target_page: str,
    target_page_assertions: list[dict[str, Any]] | None,
    event_store: ExecutionEventStore | None = None,
    case_coordinator: _CaseCoordinator | None = None,
    stage_budgets: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Run one device from navigation through every rule without cross-lane barriers."""
    machine = LaneStateMachine(event_store or ExecutionEventStore(store.home), session_id, lane["lane_id"])
    budgets = _resolve_stage_budgets(stage_budgets, probe_timeout_seconds)
    try:
        navigation = machine.run(
            "navigation",
            budgets["navigation"],
            lambda: _navigate(
                driver,
                lane["target_app_package"],
                lane["navigation_path"],
                target_page,
                target_page_assertions,
            ),
            failure_code="navigation_failed",
        )
    except LaneStageTimeout as exc:
        navigation = _stage_timeout_result(lane["lane_id"], exc)
    if not navigation.get("ok"):
        runs = _record_lane_failures(
            store,
            session_id,
            lane["target"],
            rules,
            {lane["lane_id"]: navigation},
            stage="navigation",
            note="进入目标页面失败，未执行异常规则",
        )
        if case_coordinator:
            case_coordinator.submit_many(runs)
        return {"lane_id": lane["lane_id"], "navigation": navigation, "probe": {}, "runs": runs}

    try:
        probe = machine.run(
            "target_api_probe",
            budgets["target_api_probe"],
            lambda: probe_coordinator.identify(lane, rules, driver, budgets["target_api_probe"]),
            failure_code="target_api_not_observed",
        )
    except LaneStageTimeout as exc:
        probe = _stage_timeout_result(lane["lane_id"], exc)
    if not probe.get("ok"):
        runs = _record_lane_failures(
            store,
            session_id,
            lane["target"],
            rules,
            {lane["lane_id"]: probe},
            stage="target_api_probe",
            note="目标接口探针失败",
        )
        if case_coordinator:
            case_coordinator.submit_many(runs)
        return {"lane_id": lane["lane_id"], "navigation": navigation, "probe": probe, "runs": runs}

    if probe.get("client_ip"):
        lane["client_ip"] = str(probe["client_ip"])
    runs: list[dict[str, Any]] = []
    for rule_index, rule in enumerate(rules):
        case_id = f"{session_id}:{rule['id']}"
        stage = f"rule:{rule['id']}"
        rule_started = time.monotonic()
        rule_deadline = rule_started + budgets["rule"]
        machine.start(stage, budget_seconds=budgets["rule"])
        store.proxy_state.set_active_lane(
            session_id,
            lane["lane_id"],
            {
                "target": lane["target"],
                "client_ip": lane.get("client_ip", ""),
                "rule": rule,
                "case_id": case_id,
                "activation_id": case_id,
            },
        )
        machine.events.append({"event": "rule_armed", "session_id": session_id, "lane_id": lane["lane_id"], "stage": stage, "rule_id": rule["id"]})
        try:
            run = _execute_lane_rule(
                    store,
                    session_id,
                    lane,
                    rule,
                    driver,
                    stop_on_no_request,
                    target_page=target_page,
                    target_page_assertions=target_page_assertions,
                    machine=machine,
                    stage_budgets=budgets,
                    rule_deadline=rule_deadline,
                    rule_budget=budgets["rule"],
                )
            runs.append(run)
            if case_coordinator:
                case_coordinator.submit(run)
            run_failure = (run.get("execution_gate") or {}).get("failure") or run.get("failure") or {}
            machine.finish(stage, ok=run.get("status") not in {"invalid_execution", "blocked", "failed"}, code=str(run_failure.get("code") or ""))
            if run_failure.get("code") == "stage_timeout" and rule_index + 1 < len(rules):
                remaining_rules = rules[rule_index + 1 :]
                halted_failure = {
                    **run_failure,
                    "message": "当前端阶段超时，为避免迟到动作污染后续用例，已停止该端剩余用例",
                    "next_action": "retry_current_lane_from_timed_out_case",
                }
                halted_runs = _record_lane_failures(
                    store,
                    session_id,
                    lane["target"],
                    remaining_rules,
                    {lane["lane_id"]: halted_failure},
                    stage="lane_halted_after_timeout",
                    note=halted_failure["message"],
                )
                runs.extend(halted_runs)
                if case_coordinator:
                    case_coordinator.submit_many(halted_runs)
                machine.events.append(
                    {
                        "event": "lane_halted",
                        "session_id": session_id,
                        "lane_id": lane["lane_id"],
                        "stage": stage,
                        "rule_id": rule["id"],
                        "remaining_rule_ids": [item["id"] for item in remaining_rules],
                        "code": "stage_timeout",
                    }
                )
                break
        except Exception as exc:
            machine.finish(stage, ok=False, code="rule_execution_exception", error=str(exc))
            raise
        finally:
            store.proxy_state.clear_active_lane(lane["lane_id"])
    return {"lane_id": lane["lane_id"], "navigation": navigation, "probe": probe, "runs": runs}


def _make_driver(target: str, device_serial: str = "", wda_url: str = "") -> DeviceDriver:
    """Create driver using the supplied state and inputs."""
    if target.lower() == "ios":
        try:
            return DeviceDriver(target=target, device_serial=device_serial, wda_url=wda_url)
        except TypeError:
            return DeviceDriver(target=target, device_serial=device_serial)
    return DeviceDriver(target=target, device_serial=device_serial)


def _execute_lane_rule(
    store: LocalStore,
    session_id: str,
    lane: dict[str, Any],
    rule: dict[str, Any],
    driver: DeviceDriver,
    stop_on_no_request: bool,
    target_page: str = "",
    target_page_assertions: list[dict[str, Any]] | None = None,
    machine: LaneStateMachine | None = None,
    stage_budgets: dict[str, float] | None = None,
    rule_deadline: float | None = None,
    rule_budget: float = 0,
) -> dict[str, Any]:
    """执行单端规则，请求改写成功且页面锚点仍命中后才保存有效截图。"""
    budgets = _resolve_stage_budgets(stage_budgets, DEFAULT_STAGE_BUDGETS["await_hit"])
    case_id = f"{session_id}:{rule['id']}"
    traceability = {"lane_id": lane["lane_id"], "target": lane["target"], "rule_id": rule["id"], "case_id": case_id, "activation_id": case_id, "client_ip": lane.get("client_ip", "")}
    try:
        hit = _wait_lane_rule_hit(store, session_id, lane, rule, driver, machine=machine, stage_budgets=budgets, rule_deadline=rule_deadline, rule_budget=rule_budget)
    except TypeError as exc:
        if "machine" not in str(exc) and "stage_budgets" not in str(exc):
            raise
        hit = _wait_lane_rule_hit(store, session_id, lane, rule, driver)
    except LaneStageTimeout as exc:
        failure = build_failure("stage_timeout", lane["lane_id"], exc.stage, evidence=exc.as_dict())
        return store.record_run_result(
            session_id,
            lane["target"],
            rule["id"],
            status="invalid_execution",
            review_note=f"{lane['lane_id']} {exc.stage} 阶段超时",
            execution_gate={"stage": exc.stage, "hit": False, "change_applied": False, "failure": failure},
            traceability=traceability,
        )
    try:
        page_anchor = (
            machine.run(
                "verify_page",
                _remaining_rule_budget(rule_deadline, rule_budget, budgets["verify_page"], f"rule:{rule['id']}"),
                lambda: _verify_capture_anchor(driver, target_page, target_page_assertions),
                failure_code="page_anchor_mismatch",
                rule_id=rule["id"],
            )
            if machine
            else _verify_capture_anchor(driver, target_page, target_page_assertions)
        )
    except LaneStageTimeout as exc:
        page_anchor = {"ok": False, "status": "stage_timeout", "failure": build_failure("stage_timeout", lane["lane_id"], exc.stage, evidence=exc.as_dict()), **exc.as_dict()}
    shot = store.home / "screenshots" / f"{session_id}_{lane['lane_id']}_{rule['id']}_{int(time.time())}.png"
    try:
        screenshot = (
            machine.run(
                "capture",
                _remaining_rule_budget(rule_deadline, rule_budget, budgets["capture"], f"rule:{rule['id']}"),
                lambda: driver.screenshot(shot),
                failure_code="screenshot_failed",
                rule_id=rule["id"],
            )
            if machine
            else driver.screenshot(shot)
        )
    except LaneStageTimeout as exc:
        failure = build_failure("stage_timeout", lane["lane_id"], exc.stage, evidence=exc.as_dict())
        return store.record_run_result(
            session_id,
            lane["target"],
            rule["id"],
            status="invalid_execution",
            evidence=[hit] if hit else [],
            review_note=f"{lane['lane_id']} 截图阶段超时",
            execution_gate={"stage": "capture", "hit": bool(hit), "request_count": (hit or {}).get("request_count", 0), "change_applied": bool((hit or {}).get("change_applied")), "page_anchor": page_anchor, "failure": failure},
            traceability=traceability,
        )
    except Exception:
        raise
    if hit and int(hit.get("request_count") or 0) > 0 and hit.get("change_applied") and page_anchor.get("ok"):
        result = store.record_run_result(
            session_id,
            lane["target"],
            rule["id"],
            status="pending_review",
            screenshot=screenshot,
            evidence=[hit],
            execution_gate={"stage": "rule", "hit": True, "request_count": hit.get("request_count"), "change_applied": True, "page_anchor": page_anchor},
            traceability=traceability,
        )
        return result
    if hit and int(hit.get("request_count") or 0) > 0 and hit.get("change_applied") and not page_anchor.get("ok"):
        result = store.record_run_result(
            session_id,
            lane["target"],
            rule["id"],
            status="invalid_execution",
            screenshot=screenshot,
            evidence=[hit],
            review_note=f"{lane['lane_id']} 请求已改写，但截图前页面锚点校验失败",
            execution_gate={"stage": "page_anchor", "hit": True, "request_count": hit.get("request_count"), "change_applied": True, "page_anchor": page_anchor},
            traceability=traceability,
        )
        return result
    status = "invalid_execution" if stop_on_no_request else "pending_review"
    result = store.record_run_result(
        session_id,
        lane["target"],
        rule["id"],
        status=status,
        screenshot=screenshot,
        review_note=f"{lane['lane_id']} 规则执行期间未命中目标接口",
        execution_gate={"stage": "rule", "hit": bool(hit), "request_count": (hit or {}).get("request_count", 0), "change_applied": bool((hit or {}).get("change_applied")), "page_anchor": page_anchor},
        traceability=traceability,
    )
    return result




def _wait_lane_rule_hit(
    store: LocalStore,
    session_id: str,
    lane: dict[str, Any],
    rule: dict[str, Any],
    driver: DeviceDriver,
    machine: LaneStateMachine | None = None,
    stage_budgets: dict[str, float] | None = None,
    rule_deadline: float | None = None,
    rule_budget: float = 0,
) -> dict[str, Any] | None:
    """Wait for lane rule hit using the supplied state and inputs."""
    budgets = _resolve_stage_budgets(stage_budgets, DEFAULT_STAGE_BUDGETS["await_hit"])
    store.proxy_state.clear_hit(session_id, rule["id"], lane_id=lane["lane_id"])
    trigger = (
        machine.run(
            "trigger",
            _remaining_rule_budget(rule_deadline, rule_budget, budgets["trigger"], f"rule:{rule['id']}"),
            lambda: _trigger_request(driver, lane.get("request_trigger_path") or []),
            failure_code="request_trigger_failed",
            rule_id=rule["id"],
        )
        if machine
        else _trigger_request(driver, lane.get("request_trigger_path") or [])
    )
    if not trigger.get("ok"):
        return None
    await_budget = _remaining_rule_budget(rule_deadline, rule_budget, budgets["await_hit"], f"rule:{rule['id']}")
    if machine:
        machine.start("await_hit", budget_seconds=await_budget, rule_id=rule["id"])
    deadline = time.monotonic() + await_budget
    while time.monotonic() < deadline:
        hit = store.proxy_state.read_hit(session_id, rule["id"], lane_id=lane["lane_id"])
        if hit:
            if machine:
                machine.finish("await_hit", ok=True, rule_id=rule["id"], request_count=hit.get("request_count", 0))
            return hit
        time.sleep(min(0.1, max(0.001, deadline - time.monotonic())))
    if machine:
        machine.finish("await_hit", ok=False, code="target_request_not_hit", rule_id=rule["id"])
    return None


def _record_lane_failures(
    store: LocalStore,
    session_id: str,
    target: str,
    rules: list[dict[str, Any]],
    failures: dict[str, dict[str, Any]],
    stage: str,
    note: str,
) -> list[dict[str, Any]]:
    """Record lane failures using the supplied state and inputs."""
    records: list[dict[str, Any]] = []
    for rule in rules:
        for lane_id, failure in failures.items():
            records.append(
                store.record_run_result(
                    session_id,
                    lane_id,
                    rule["id"],
                    status="invalid_execution",
                    review_note=f"{lane_id}: {failure.get('message') or note}",
                    execution_gate={"stage": stage, "hit": False, "failure": failure},
                    traceability={"lane_id": lane_id, "rule_id": rule["id"]},
                )
            )
    return records




def _rule_count_gate(rule: dict[str, Any], lanes: list[dict[str, Any]], rule_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Handle rule count gate using the supplied state and inputs."""
    lane_counts: dict[str, dict[str, Any]] = {
        lane["lane_id"]: {"request_count": 0, "change_applied": False, "status": "not_executed"}
        for lane in lanes
    }
    for result in rule_results:
        traceability = result.get("traceability") or {}
        lane_id = str(traceability.get("lane_id") or "")
        if lane_id not in lane_counts:
            continue
        gate = result.get("execution_gate") or {}
        page_anchor = gate.get("page_anchor") or {"ok": False, "verified": False, "skipped": True}
        request_signature = _request_evidence_signature(result)
        lane_counts[lane_id] = {
            "request_count": int(gate.get("request_count") or 0),
            "change_applied": bool(gate.get("change_applied")),
            "page_anchor_ok": bool(page_anchor.get("ok") and page_anchor.get("verified") and not page_anchor.get("skipped")),
            "request_signature": request_signature,
            "status": result.get("status"),
        }
    actual = sum(1 for item in lane_counts.values() if item["request_count"] > 0 and item["change_applied"] and item.get("page_anchor_ok", False))
    expected = len(lanes)
    signatures = {lane_id: item.get("request_signature") or {} for lane_id, item in lane_counts.items()}
    serialized_signatures = {json.dumps(signature, ensure_ascii=False, sort_keys=True) for signature in signatures.values()}
    request_consistency = {
        "passed": actual == expected and len(signatures) == expected and len(serialized_signatures) == 1,
        "signatures": signatures,
    }
    return {
        "rule_id": rule.get("id"),
        "case_name": rule.get("case_name"),
        "expected_success_count": expected,
        "actual_success_count": actual,
        "passed": actual == expected and request_consistency["passed"],
        "lane_counts": lane_counts,
        "request_consistency": request_consistency,
    }


def _request_evidence_signature(result: dict[str, Any]) -> dict[str, Any]:
    """提取用于三端交叉校验的请求与实际修改签名。"""
    evidence = next(iter(result.get("evidence") or []), {})
    changes = []
    for item in [*(evidence.get("patch_evidence") or []), *(evidence.get("mutation_evidence") or [])]:
        if not item.get("applied"):
            continue
        changes.append(
            {
                "field": str(item.get("resolved_field") or item.get("field") or ""),
                "action": str(item.get("action") or ""),
                "after_exists": bool(item.get("after_exists", True)),
                "after": item.get("after"),
            }
        )
    changes.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    request_path = str(evidence.get("request_path") or "")
    if not request_path:
        request_path = str(evidence.get("url") or evidence.get("api") or "").split("?", 1)[0]
    return {
        "method": str(evidence.get("method") or "").upper(),
        "request_path": request_path,
        "api": str(evidence.get("api") or ""),
        "changes": changes,
    }


def _target_api_probe(store: LocalStore, session_id: str, target: str, rules: list[dict[str, Any]], driver: DeviceDriver, timeout: float, request_trigger_path: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Handle target api probe using the supplied state and inputs."""
    apis = [
        {
            "api": str(rule.get("api") or ""),
            "host": str(rule.get("host") or ""),
            "method": str(rule.get("method") or rule.get("http_method") or "").upper(),
        }
        for rule in rules
        if rule.get("api")
    ]
    if not apis:
        return {"ok": False, "apis": apis, "message": "未配置目标接口，禁止跳过接口探针", "code": "rule_api_required"}
    store.proxy_state.clear_recent_requests(session_id)
    store.proxy_state.set_probe_lanes(
        session_id,
        {target: {"target": target, "apis": apis, "client_ip": ""}},
    )
    trigger = _trigger_request(driver, request_trigger_path or [])
    if not trigger.get("ok"):
        return {"ok": False, "message": trigger.get("message"), "apis": apis, "trigger": trigger}
    deadline = time.time() + timeout
    while time.time() < deadline:
        recent = store.proxy_state.read_recent_requests(session_id)
        if any(row.get("matched_apis") for row in recent):
            return {"ok": True, "apis": apis, "recent_requests": recent}
        time.sleep(0.5)
    recent = store.proxy_state.read_recent_requests(session_id)
    if recent:
        return {"ok": False, "message": "代理连通性探针通过，但未看到目标接口", "apis": apis, "recent_requests": recent}
    return {"ok": False, "message": "代理未捕获到流量，请先确认 WLAN 代理指向 MCP 提示端口", "apis": apis, "recent_requests": recent}


def _execute_rule(
    store: LocalStore,
    session_id: str,
    target: str,
    rule: dict[str, Any],
    driver: DeviceDriver,
    stop_on_no_request: bool,
    target_page: str = "",
    target_page_assertions: list[dict[str, Any]] | None = None,
    request_trigger_path: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Handle execute rule using the supplied state and inputs."""
    store.proxy_state.clear_hit(session_id, rule["id"], lane_id=target)
    store.proxy_state.set_active(session_id, target, rule)
    trigger = _trigger_request(driver, request_trigger_path or [])
    if not trigger.get("ok"):
        return store.record_run_result(
            session_id,
            target,
            rule["id"],
            status="invalid_execution",
            review_note=trigger.get("message", "缺少请求触发路径"),
            execution_gate={"stage": "request_trigger", "execution_state": "request_trigger_missing", "trigger": trigger},
        )
    deadline = time.time() + 12
    hit: dict[str, Any] | None = None
    while time.time() < deadline:
        hit = store.proxy_state.read_hit(session_id, rule["id"], lane_id=target)
        if hit:
            break
        time.sleep(0.5)
    page_anchor = _verify_capture_anchor(driver, target_page, target_page_assertions)
    shot = store.home / "screenshots" / f"{session_id}_{rule['id']}_{int(time.time())}.png"
    screenshot = driver.screenshot(shot)
    change_applied = bool((hit or {}).get("change_applied"))
    request_count = int((hit or {}).get("request_count") or (1 if hit else 0))
    if hit and change_applied and request_count > 0 and page_anchor.get("ok") and page_anchor.get("verified"):
        return store.record_run_result(
            session_id,
            target,
            rule["id"],
            status="pending_review",
            screenshot=screenshot,
            evidence=[hit],
            execution_gate={"stage": "rule", "hit": True, "request_count": request_count, "change_applied": True, "page_anchor": page_anchor},
        )
    if hit and change_applied and not (page_anchor.get("ok") and page_anchor.get("verified")):
        return store.record_run_result(
            session_id,
            target,
            rule["id"],
            status="invalid_execution",
            screenshot=screenshot,
            evidence=[hit],
            review_note="请求已改写，但截图前页面锚点校验失败",
            execution_gate={"stage": "page_anchor", "hit": True, "request_count": request_count, "change_applied": True, "page_anchor": page_anchor},
        )
    status = "invalid_execution" if stop_on_no_request else "pending_review"
    return store.record_run_result(
        session_id,
        target,
        rule["id"],
        status=status,
        screenshot=screenshot,
        review_note="规则执行期间未命中目标接口",
        execution_gate={"stage": "rule", "hit": bool(hit), "request_count": request_count, "change_applied": change_applied, "page_anchor": page_anchor},
    )


def _verify_capture_anchor(
    driver: DeviceDriver,
    target_page: str,
    target_page_assertions: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Require a verified target-page anchor immediately before evidence capture."""
    if not target_page and not target_page_assertions:
        return {"ok": False, "verified": False, "skipped": True, "message": "未配置页面锚点，禁止截图进入有效执行链路"}
    return _verify_target_page(driver, target_page, target_page_assertions)


def _result(store: LocalStore, session_id: str, imported: dict[str, Any], preflight: dict[str, Any], runs: list[dict[str, Any]], report: dict[str, Any], status: str, **extra: Any) -> dict[str, Any]:
    """Build the stable Runner response with retained-proxy policy and a manual-close reminder."""
    return {
        "ok": status == "reviewed",
        "status": status,
        "session_id": session_id,
        "imported": imported,
        "summary": store.session_summary(session_id),
        "preflight": preflight,
        "proxy_instruction": _proxy_instruction_from_preflight(preflight),
        "runs": runs,
        "report": report,
        "proxy_lifecycle": {
            "started_before_navigation": True,
            "auto_stop": False,
            "manual_cleanup_required": True,
            "user_reminder": MANUAL_PROXY_REMINDER,
            "phone_proxy_mutation_allowed": True,
            "phone_proxy_policy": "managed_snapshot_apply_verify_retain",
        },
        "transport_contract": "single_mcp_call_internal_workers",
        **extra,
    }


def _proxy_instruction_from_preflight(preflight: dict[str, Any]) -> dict[str, Any] | None:
    """Handle proxy instruction from preflight using the supplied state and inputs."""
    if preflight.get("proxy_instruction"):
        return preflight.get("proxy_instruction")
    if preflight.get("proxy_instructions"):
        return {"targets": preflight.get("proxy_instructions")}
    lanes = preflight.get("lanes") or {}
    if lanes:
        return {"targets": {target: lane.get("proxy_instruction") for target, lane in lanes.items()}}
    return None


def _workspace_locked(function: Any) -> Any:
    """Serialize formal runs that share mutable proxy state in one workspace."""
    signature = inspect.signature(function)

    @wraps(function)
    def locked(*args: Any, **kwargs: Any) -> dict[str, Any]:
        """Acquire the workspace lock before imports, preflight, sessions, or proxy mutations."""
        from mobile_auto_mcp.proxy.recovery import WorkspaceBusyError, WorkspaceRunLock

        arguments = signature.bind_partial(*args, **kwargs).arguments
        home = workspace_home(
            base_home=str(arguments.get("base_home") or "") or None,
            tenant_id=str(arguments.get("tenant_id") or ""),
            workspace_id=str(arguments.get("workspace_id") or ""),
            app_id=str(arguments.get("app_id") or ""),
        )
        lock = WorkspaceRunLock(home, owner=f"run-{uuid4().hex}")
        try:
            lock.acquire()
        except WorkspaceBusyError as exc:
            return {
                "ok": False,
                "status": "workspace_busy",
                "session_id": "",
                "runs": [],
                "report": {},
                "message": str(exc),
            }
        try:
            return function(*args, **kwargs)
        finally:
            lock.release()

    return locked


# Keep the public signature for MCP schema generation while enforcing the cross-process lock at the outermost boundary.
run_cases = _workspace_locked(run_cases)
