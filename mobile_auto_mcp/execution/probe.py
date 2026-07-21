"""Fresh per-run proxy probing and device-to-client-IP binding."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from mobile_auto_mcp.state.storage import LocalStore

if TYPE_CHECKING:
    from mobile_auto_mcp.execution.devices import DeviceDriver


class LaneProbeCoordinator:
    """Serialize fresh lane probes and retain only bindings observed in the current run."""

    def __init__(
        self,
        store: LocalStore,
        session_id: str,
        trigger_request: Callable[["DeviceDriver", list[dict[str, Any]]], dict[str, Any]],
    ) -> None:
        """Bind the coordinator to one session and the UI action used to trigger traffic."""
        self.store = store
        self.session_id = session_id
        self._trigger_request = trigger_request
        self._lock = threading.Lock()
        self._client_ips: dict[str, str] = {}

    def prepare(self, lanes: list[dict[str, Any]], rules: list[dict[str, Any]]) -> None:
        """Clear current-session evidence and leave all lanes unbound for fresh observation."""
        apis = _request_contracts(rules)
        if not apis:
            return
        for lane in lanes:
            lane["client_ip"] = ""
            lane.pop("client_ip_hint", None)
            lane.pop("probe_preconfigured", None)
        self.store.proxy_state.clear_recent_requests(self.session_id)

    def identify(
        self,
        lane: dict[str, Any],
        rules: list[dict[str, Any]],
        driver: "DeviceDriver",
        timeout: float,
    ) -> dict[str, Any]:
        """Probe one lane and add its address only after an unambiguous current-run match."""
        with self._lock:
            try:
                result = probe_lane(
                    self.store,
                    self.session_id,
                    lane,
                    rules,
                    driver,
                    timeout,
                    trigger_request=self._trigger_request,
                    known_client_ips=dict(self._client_ips),
                )
                if result.get("ok") and result.get("client_ip"):
                    self._client_ips[lane["lane_id"]] = str(result["client_ip"])
                return result
            finally:
                self.store.proxy_state.clear_probe()


def probe_lane(
    store: LocalStore,
    session_id: str,
    lane: dict[str, Any],
    rules: list[dict[str, Any]],
    driver: "DeviceDriver",
    timeout: float,
    *,
    trigger_request: Callable[["DeviceDriver", list[dict[str, Any]]], dict[str, Any]],
    known_client_ips: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Trigger target traffic and bind a lane only to a freshly observed matching request."""
    apis = _request_contracts(rules)
    lane_id = lane["lane_id"]
    if not apis:
        return {"ok": True, "lane_id": lane_id, "apis": apis, "message": "未配置目标接口，跳过接口探针"}
    if not lane.get("probe_preconfigured"):
        store.proxy_state.clear_recent_requests(session_id)
    probe_lanes = {
        known_lane: {"target": known_lane, "apis": apis, "client_ip": client_ip}
        for known_lane, client_ip in (known_client_ips or {}).items()
        if client_ip
    }
    probe_lanes[lane_id] = {"target": lane["target"], "apis": apis, "client_ip": lane.get("client_ip", "")}
    if not lane.get("probe_preconfigured"):
        store.proxy_state.set_probe_lanes(session_id, probe_lanes)
    trigger = trigger_request(driver, lane.get("request_trigger_path") or [])
    if not trigger.get("ok"):
        return {"ok": False, "lane_id": lane_id, "apis": apis, "message": trigger.get("message"), "trigger": trigger}
    deadline = time.time() + timeout
    while time.time() < deadline:
        recent = [row for row in store.proxy_state.read_recent_requests(session_id) if row.get("lane_id") == lane_id]
        if any(row.get("matched_apis") for row in recent):
            return {
                "ok": True,
                "lane_id": lane_id,
                "apis": apis,
                "client_ip": recent[-1].get("client_ip", ""),
                "recent_requests": recent,
            }
        time.sleep(0.5)
    recent = [row for row in store.proxy_state.read_recent_requests(session_id) if row.get("lane_id") == lane_id]
    message = "代理未捕获到流量，请确认该端 WLAN 代理指向共享 mitmproxy 端口"
    if recent:
        message = "代理捕获到该端流量，但未看到目标接口"
    return {"ok": False, "lane_id": lane_id, "apis": apis, "recent_requests": recent, "message": message}


def _request_contracts(rules: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Build stable exact-match probe contracts without dropping host or method fields."""
    contracts: list[dict[str, str]] = []
    for rule in rules:
        contract = {
            "api": str(rule.get("api") or ""),
            "host": str(rule.get("host") or ""),
            "method": str(rule.get("method") or rule.get("http_method") or "").upper(),
        }
        if contract["api"] and contract not in contracts:
            contracts.append(contract)
    return contracts
