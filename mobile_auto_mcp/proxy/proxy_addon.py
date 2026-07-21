"""mitmproxy addon that applies the active abnormal rule to matching JSON APIs."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from mitmproxy import http

from mobile_auto_mcp.proxy.mutation_engine import apply_mutations_with_evidence
from mobile_auto_mcp.proxy.matching import request_matches_rule
from mobile_auto_mcp.proxy.patch_engine import apply_patches_with_evidence
from mobile_auto_mcp.proxy.proxy_state import ProxyState
from mobile_auto_mcp.state.storage import default_home


class MobileAutoAddon:
    """Apply rules shared through ProxyState and record request/hit evidence."""

    def __init__(self) -> None:
        """连接 mitmproxy addon 与当前工作区的共享代理状态。"""
        self.state = ProxyState(default_home())

    def request(self, flow: http.HTTPFlow) -> None:
        """Bind only an exact rule-matching request to the current activation."""
        active = self.state.read_active()
        if not active:
            return
        context = _request_context(
            active, _client_ip(flow), flow.request.method, flow.request.pretty_url
        )
        if context and request_matches_rule(
            context, flow.request.pretty_url, flow.request.method
        ):
            # 只给精确 host/path/method 命中的请求绑定 activation，隔离迟到或相似 URL 响应。
            flow.metadata["mobile_auto_mcp_request_context"] = context

    def response(self, flow: http.HTTPFlow) -> None:
        """Mutate an exactly matched JSON response and persist its audit evidence."""
        url = flow.request.pretty_url
        path = flow.request.path or url
        client_ip = _client_ip(flow)
        probe = self.state.read_probe()
        active = self.state.read_active()
        event = {
            "event": "response",
            "client_ip": client_ip,
            "url": url,
            "path": path,
            "method": flow.request.method,
            "status_code": flow.response.status_code,
            "matched_apis": [],
            "lane_id": "",
            "rule_id": "",
            "mutated": False,
        }
        if probe:
            probe_event = self._record_probe(probe, flow, path, client_ip)
            event.update(
                {k: v for k, v in probe_event.items() if v not in ("", [], None)}
            )
        if not active:
            self._record_event(probe or {}, event)
            return
        request_context = flow.metadata.get("mobile_auto_mcp_request_context") or {}
        if not _request_context_matches(active, request_context):
            event["reason"] = "request_activation_mismatch"
            event["activation_id"] = str(request_context.get("activation_id") or "")
            self._record_event(active, event)
            return
        lane_id = str(request_context.get("lane_id") or "")
        lane = (active.get("lanes") or {}).get(lane_id) or {}
        rule = lane.get("rule") or active.get("rule") or {}
        api = str(rule.get("api") or "").strip()
        event["lane_id"] = lane_id
        event["rule_id"] = str(rule.get("id") or "")
        if not rule.get("id"):
            event["reason"] = "lane_not_identified"
            self._record_event(active, event)
            return
        if not request_matches_rule(rule, url, flow.request.method):
            event["reason"] = "request_rule_mismatch"
            self._record_event(active, event)
            return
        payload = self._json_payload(flow)
        if payload is None:
            event["reason"] = "non_json_response"
            self._record_event(active, event)
            return
        patched, patch_evidence = apply_patches_with_evidence(
            payload,
            patches=rule.get("patches") or [],
            fixtures=rule.get("fixtures") or [],
            mock_sources=rule.get("mock_sources") or [],
        )
        mutated, mutation_evidence = apply_mutations_with_evidence(
            patched, rule.get("mutations") or []
        )
        change_applied = any(
            item.get("applied") for item in [*patch_evidence, *mutation_evidence]
        )
        if not change_applied:
            event["reason"] = "no_patch_or_mutation_applied"
            event["patch_evidence"] = patch_evidence
            event["mutation_evidence"] = mutation_evidence
            self._record_event(active, event)
            return
        modified_response = self.state.record_modified_response(
            str(active.get("session_id") or ""),
            {
                "lane_id": lane_id,
                "rule_id": str(rule.get("id") or ""),
                "activation_id": str(
                    lane.get("activation_id") or active.get("activation_id") or ""
                ),
                "client_ip": client_ip,
                "url": url,
                "method": flow.request.method,
                "status_code": flow.response.status_code,
                "modified_response": mutated,
            },
        )
        flow.response.text = json.dumps(mutated, ensure_ascii=False)
        self.state.record_hit(
            str(active.get("session_id") or ""),
            str(rule.get("id") or ""),
            {
                "lane_id": lane_id,
                "client_ip": client_ip,
                "url": url,
                "method": flow.request.method,
                "status_code": flow.response.status_code,
                "api": api,
                "activation_id": str(
                    lane.get("activation_id") or active.get("activation_id") or ""
                ),
                "request_path": str(request_context.get("request_path") or ""),
                "change_applied": True,
                "modified_response_id": modified_response["id"],
                "modified_response_sequence": modified_response["sequence"],
                "patch_evidence": patch_evidence,
                "mutation_evidence": mutation_evidence,
            },
            lane_id=lane_id,
        )
        event.update(
            {
                "matched_apis": [api] if api else [],
                "mutated": True,
                "modified_response_id": modified_response["id"],
                "modified_response_sequence": modified_response["sequence"],
            }
        )
        self._record_event(active, event)

    def _record_probe(
        self, probe: dict[str, Any], flow: http.HTTPFlow, path: str, client_ip: str
    ) -> dict[str, Any]:
        """Record only exact target API matches while retaining all request evidence."""
        lane_id, lane = _select_lane(probe, client_ip)
        apis = lane.get("apis") or probe.get("apis") or []
        matched = [
            api
            for api in apis
            if request_matches_rule(
                api if isinstance(api, dict) else {"api": api},
                flow.request.pretty_url,
                flow.request.method,
            )
        ]
        self.state.record_recent_request(
            str(probe.get("session_id") or ""),
            {
                "lane_id": lane_id,
                "client_ip": client_ip,
                "url": flow.request.pretty_url,
                "path": path,
                "method": flow.request.method,
                "status_code": flow.response.status_code,
                "matched_apis": matched,
            },
        )
        return {"lane_id": lane_id, "client_ip": client_ip, "matched_apis": matched}

    def _json_payload(self, flow: http.HTTPFlow) -> Any | None:
        """Decode a JSON response and reject content that cannot be safely mutated."""
        content_type = flow.response.headers.get("content-type", "")
        if (
            "json" not in content_type.lower()
            and not flow.response.text.strip().startswith(("{", "["))
        ):
            return None
        try:
            return json.loads(flow.response.text)
        except (TypeError, ValueError):
            return None

    def _record_event(self, state: dict[str, Any], event: dict[str, Any]) -> None:
        """Append proxy evidence only when it can be attributed to a session."""
        session_id = str(state.get("session_id") or "")
        if session_id:
            self.state.record_event(session_id, event)


def _client_ip(flow: http.HTTPFlow) -> str:
    """Extract the peer IP used to isolate traffic from concurrent device lanes."""
    peername = getattr(flow.client_conn, "peername", None)
    if isinstance(peername, tuple) and peername:
        return str(peername[0])
    return ""


def _select_lane(state: dict[str, Any], client_ip: str) -> tuple[str, dict[str, Any]]:
    """Resolve exactly one device lane without guessing between ambiguous peers."""
    lanes = state.get("lanes") or {}
    for lane_id, lane in lanes.items():
        if lane.get("client_ip") and lane.get("client_ip") == client_ip:
            return str(lane_id), lane
    if len(lanes) == 1:
        lane_id, lane = next(iter(lanes.items()))
        if lane.get("client_ip") and client_ip and lane.get("client_ip") != client_ip:
            return "", {}
        return str(lane_id), lane
    if client_ip and client_ip in lanes:
        return client_ip, lanes[client_ip]
    unbound = [
        (str(lane_id), lane)
        for lane_id, lane in lanes.items()
        if not lane.get("client_ip")
    ]
    if client_ip and len(unbound) == 1:
        return unbound[0]
    return "", {}


def _request_context(
    active: dict[str, Any] | None, client_ip: str, method: str, url: str
) -> dict[str, Any]:
    """Build immutable activation ownership plus the rule's exact match fields."""
    if not active:
        return {}
    lane_id, lane = _select_lane(active, client_ip)
    rule = lane.get("rule") or {}
    if not lane_id or not rule.get("id"):
        return {}
    return {
        "session_id": str(active.get("session_id") or ""),
        "activation_id": str(
            lane.get("activation_id") or active.get("activation_id") or ""
        ),
        "lane_id": lane_id,
        "client_ip": client_ip,
        "rule_id": str(rule.get("id") or ""),
        "api": str(rule.get("api") or ""),
        "host": str(rule.get("host") or ""),
        "method": str(rule.get("method") or rule.get("http_method") or "").upper(),
        "request_method": str(method or "").upper(),
        "request_path": urlsplit(url).path,
    }


def _request_context_matches(
    active: dict[str, Any] | None, context: dict[str, Any]
) -> bool:
    """Reject responses whose session, activation, lane, rule, or peer changed."""
    if not active or not context:
        return False
    if context.get("session_id") != active.get("session_id"):
        return False
    lane = (active.get("lanes") or {}).get(str(context.get("lane_id") or "")) or {}
    rule = lane.get("rule") or {}
    activation_id = str(lane.get("activation_id") or active.get("activation_id") or "")
    expected_client_ip = str(lane.get("client_ip") or "")
    return bool(
        rule.get("id")
        and activation_id == str(context.get("activation_id") or "")
        and str(rule.get("id")) == str(context.get("rule_id") or "")
        and (
            not expected_client_ip
            or expected_client_ip == str(context.get("client_ip") or "")
        )
    )


addons = [MobileAutoAddon()]
