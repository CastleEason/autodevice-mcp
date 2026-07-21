"""Regression tests for importing and persisting exact request contracts."""

from __future__ import annotations

from mobile_auto_mcp.cases.parser import extract_rules_from_markdown
from mobile_auto_mcp.state.storage import LocalStore


def test_markdown_import_preserves_full_url_host_and_method() -> None:
    """验证 Markdown 中的 METHOD + URL 会完整进入规则，而不是只留下 Path。"""
    rules = extract_rules_from_markdown(
        """# Player\nAPI: GET https://api.example.test/v1/player/profile\n- `note` 字段缺失\n"""
    )

    assert rules[0]["api"] == "https://api.example.test/v1/player/profile"
    assert rules[0]["host"] == "api.example.test"
    assert rules[0]["method"] == "GET"


def test_store_round_trip_preserves_and_updates_request_contract(tmp_path) -> None:
    """验证规则持久化与人工覆盖都保留精确 Host/Path/Method。"""
    store = LocalStore(tmp_path)
    saved = store.save_rules(
        [
            {
                "id": "rule-a",
                "case_name": "note missing",
                "api": "/v1/player/profile",
                "host": "api.old.test",
                "method": "GET",
                "mutations": [{"field": "note", "action": "missing"}],
            }
        ]
    )

    assert saved[0]["host"] == "api.old.test"
    assert saved[0]["method"] == "GET"

    updated = store.update_rule_request_contracts(
        ["rule-a"],
        api_override="/v2/player/profile",
        host_override="api.new.test",
        method_override="POST",
    )

    assert updated[0]["api"] == "/v2/player/profile"
    assert updated[0]["host"] == "api.new.test"
    assert updated[0]["method"] == "POST"
