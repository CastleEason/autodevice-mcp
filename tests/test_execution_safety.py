"""Regression tests for hard execution gates and exact API matching."""

from __future__ import annotations

from pathlib import Path

import pytest

from mobile_auto_mcp.execution.contracts import derive_single_run_status, validate_execution_contract
from mobile_auto_mcp.execution import runner
from mobile_auto_mcp.proxy.matching import request_matches_rule
from mobile_auto_mcp.state.storage import LocalStore, workspace_home


def _rule(**overrides: object) -> dict[str, object]:
    """Build a minimal valid mutation rule and allow one test to alter only its relevant field."""
    return {
        "id": "rule-a",
        "api": "https://api.example.test/v1/player/profile",
        "method": "GET",
        "mutations": [{"field": "note", "action": "missing"}],
        "enabled": True,
        **overrides,
    }


@pytest.mark.parametrize(
    ("rules", "requested_rule_ids", "target_page", "assertions", "code"),
    [
        ([], None, "Profile", None, "execution_rules_required"),
        ([_rule()], ["rule-missing"], "Profile", None, "requested_rules_missing"),
        ([_rule(api="")], None, "Profile", None, "rule_api_required"),
        ([_rule(api="/v1/player/profile", host="")], None, "Profile", None, "rule_host_required"),
        ([_rule(method="")], None, "Profile", None, "rule_method_required"),
        ([_rule(mutations=[])], None, "Profile", None, "rule_mutation_required"),
        ([_rule(mutations=[{}])], None, "Profile", None, "rule_mutation_required"),
        ([_rule(mutations=[{"field": "note", "action": "unsupported"}])], None, "Profile", None, "rule_mutation_required"),
        ([_rule(mutations=[], patches=[{"field": "note", "action": "unsupported"}])], None, "Profile", None, "rule_mutation_required"),
        ([_rule(mutations=[], fixtures=[{"value": "scalar"}])], None, "Profile", None, "rule_mutation_required"),
        ([_rule(mutations=[], mock_sources=[{"value": None}])], None, "Profile", None, "rule_mutation_required"),
        ([_rule(patches=[{"field": "note", "action": "delete"}])], None, "Profile", None, "rule_mutation_required"),
        ([_rule()], None, "", None, "page_anchor_required"),
    ],
)
def test_invalid_execution_contract_blocks_before_device_work(
    rules: list[dict[str, object]],
    requested_rule_ids: list[str] | None,
    target_page: str,
    assertions: list[dict[str, object]] | None,
    code: str,
) -> None:
    """验证空规则、错规则、空 API、无改写资产或无页面锚点均返回稳定硬阻断代码。"""
    result = validate_execution_contract(
        rules,
        requested_rule_ids=requested_rule_ids,
        target_page=target_page,
        target_page_assertions=assertions,
    )

    assert result["ok"] is False
    assert code in {blocker["code"] for blocker in result["blockers"]}


def test_exact_request_match_requires_host_path_and_method() -> None:
    """验证同前缀、同路径异 Host、同 URL 异 Method 均不能触发响应改写。"""
    rule = _rule()

    assert request_matches_rule(rule, "https://api.example.test/v1/player/profile?player=1", "GET") is True
    assert request_matches_rule(rule, "https://other.example.test/v1/player/profile", "GET") is False
    assert request_matches_rule(rule, "https://api.example.test/v1/player/profile/extra", "GET") is False
    assert request_matches_rule(rule, "https://api.example.test/v1/player/profiles", "GET") is False
    assert request_matches_rule(rule, "https://api.example.test/v1/player/profile", "POST") is False


def test_request_match_rejects_incomplete_stored_contracts() -> None:
    """验证缺失 Host 或 Method 的旧规则不会在代理层退化为通配规则。"""
    url = "https://api.example.test/v1/player/profile"

    assert request_matches_rule({"api": "/v1/player/profile", "method": "GET"}, url, "GET") is False
    assert request_matches_rule({"api": url}, url, "GET") is False


def test_valid_single_device_evidence_still_waits_for_final_review() -> None:
    """验证接口改写和页面锚点通过仅代表可复核，不能直接生成最终 finished 成功态。"""
    result = derive_single_run_status(
        [
            {
                "rule_id": "rule-a",
                "status": "pending_review",
                "execution_gate": {
                    "change_applied": True,
                    "page_anchor": {"ok": True, "verified": True},
                },
            }
        ]
    )

    assert result["ok"] is False
    assert result["execution_ok"] is True
    assert result["status"] == "awaiting_review"


def test_missing_or_skipped_page_anchor_keeps_single_device_result_partial() -> None:
    """验证 page_anchor 缺失或 skipped 不能借默认值进入有效执行结果。"""
    result = derive_single_run_status(
        [
            {
                "rule_id": "rule-a",
                "status": "pending_review",
                "execution_gate": {"change_applied": True, "page_anchor": {"ok": True, "verified": False, "skipped": True}},
            }
        ]
    )

    assert result["ok"] is False
    assert result["status"] == "partial"
    assert "page_anchor_unproven" in {blocker["code"] for blocker in result["blockers"]}


def test_runner_rejects_invalid_contract_before_preflight_or_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Runner 的硬门禁位于前置检查和正式 Session 之前，而不只是一个未接线的纯函数。"""
    base_home = str(tmp_path)

    def unexpected_preflight(**kwargs: object) -> dict[str, object]:
        """Fail immediately if an invalid contract reaches any device-readiness work."""
        raise AssertionError(f"preflight must not run: {kwargs}")

    monkeypatch.setattr(runner, "prepare_managed_environment", unexpected_preflight)

    result = runner.run_cases(
        app_id="contract-test",
        base_home=base_home,
        target="android",
        target_page="Profile",
        proxy_required=False,
    )
    store = LocalStore(workspace_home(base_home=base_home, app_id="contract-test"))

    assert result["status"] == "invalid_execution_contract"
    assert store.list_sessions() == []
