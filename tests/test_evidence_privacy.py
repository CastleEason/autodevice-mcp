"""Regression tests for private report evidence and safe report hosting defaults."""

from __future__ import annotations

from mobile_auto_mcp.proxy.proxy_state import ProxyState
from mobile_auto_mcp.reports.server import ReportServerManager


def test_proxy_events_and_hits_redact_query_values_and_mutation_values(tmp_path) -> None:
    """验证 URL 参数及改写前后原值不会进入报告可导出的代理证据。"""
    state = ProxyState(tmp_path)
    evidence = {
        "url": "https://api.example.test/v1/profile?token=secret&id=private-user",
        "patch_evidence": [{"field": "note", "before": "private note", "after": "changed", "applied": True}],
    }

    state.record_event("session-a", evidence)
    state.record_hit("session-a", "rule-a", evidence)

    serialized = str(state.read_events("session-a")) + str(state.list_hits("session-a"))
    assert "secret" not in serialized
    assert "private-user" not in serialized
    assert "private note" not in serialized
    assert "changed" not in serialized


def test_report_server_defaults_to_loopback(tmp_path) -> None:
    """验证报告服务默认只监听本机，LAN 暴露必须由调用方显式选择。"""
    manager = ReportServerManager(tmp_path)

    assert manager.host == "127.0.0.1"


def test_modified_response_redacts_common_personal_fields(tmp_path) -> None:
    """验证完整响应保留 JSON 结构，但常见身份、姓名和电话值不会写入报告资产。"""
    state = ProxyState(tmp_path)

    saved = state.record_modified_response(
        "session-a",
        {"modified_response": {"user_id": "42", "name": "Private Name", "phone": "13800000000", "note": "ok"}},
    )

    response = saved["modified_response"]
    assert response["user_id"] == "***REDACTED***"
    assert response["name"] == "***REDACTED***"
    assert response["phone"] == "***REDACTED***"
    assert response["note"] == "ok"
