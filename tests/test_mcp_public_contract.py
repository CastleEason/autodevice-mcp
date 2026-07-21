"""Public MCP schema checks for newly added safety controls."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from mobile_auto_mcp.mcp_tools import register_all_tools


def _tools() -> dict[str, Any]:
    """Register tools in isolation and expose their schemas by stable public name."""
    mcp = FastMCP("public-contract")
    register_all_tools(mcp)
    return {tool.name: tool for tool in mcp._tool_manager.list_tools()}


def test_public_contract_exposes_safe_recovery_and_host_override() -> None:
    """验证组员可通过 MCP 恢复代理，且显式 Host 只能作为 Runner 的可验证输入。"""
    tools = _tools()

    assert "restore_retained_proxy" in tools
    run_schema = tools["run_cases"].parameters
    assert "proxy_host" in (run_schema.get("properties") or {})
    override_schema = tools["apply_case_asset_overrides"].parameters
    override_fields = override_schema.get("properties") or {}
    assert "host_override" in override_fields
    assert "method_override" in override_fields


def test_visual_tool_describes_precheck_instead_of_final_verdict() -> None:
    """验证公开工具说明不会再暗示 Pillow 预检可以产生最终通过或失败。"""
    description = str(_tools()["run_visual_comparison"].description or "").lower()

    assert "non-final" in description
    assert "precheck" in description
