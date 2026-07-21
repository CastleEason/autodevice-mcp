"""Readable proxy trace and diagnosis helpers."""

from __future__ import annotations

from typing import Any

from mobile_auto_mcp.state.storage import LocalStore


def build_proxy_trace(store: LocalStore, session_id: str = "", limit: int = 50) -> dict[str, Any]:
    """Return recent requests, rule hits, and a compact diagnosis."""
    recent = store.proxy_state.read_recent_requests(session_id)
    hits = store.proxy_state.list_hits(session_id=session_id, limit=limit)
    events = store.proxy_state.read_events(session_id=session_id, limit=limit * 4)
    active = store.proxy_state.read_active()
    probe = store.proxy_state.read_probe()
    return {
        "session_id": session_id,
        "active_rule": active,
        "probe": probe,
        "recent_requests": recent[-limit:],
        "hits": hits,
        "events": events,
        "logs": {
            "events_jsonl": str(store.home / "proxy" / "mitmproxy_events.jsonl"),
            "stdout": str(store.home / "proxy" / "mitmdump_stdout.log"),
            "stderr": str(store.home / "proxy" / "mitmdump_stderr.log"),
        },
        "diagnosis": diagnose_proxy_trace(recent, hits),
    }


def diagnose_proxy_trace(recent: list[dict[str, Any]], hits: list[dict[str, Any]]) -> dict[str, Any]:
    """Handle diagnose proxy trace using the supplied state and inputs."""
    if hits:
        return {"status": "hit", "message": "代理已命中目标接口并完成规则注入"}
    if recent:
        matched = [row for row in recent if row.get("matched_apis")]
        if matched:
            return {"status": "api_seen_no_hit", "message": "已看到目标接口，但未产生规则命中，请检查规则 api 与响应类型"}
        return {"status": "proxy_connected_api_missing", "message": "代理有流量，但未看到目标接口"}
    return {"status": "no_traffic", "message": "代理未捕获到流量，请确认手机 WLAN 代理指向本机端口"}
