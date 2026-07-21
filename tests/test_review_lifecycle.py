"""Regression tests for the visual-precheck and final-review lifecycle."""

from __future__ import annotations

from pathlib import Path

import pytest

from mobile_auto_mcp.state.storage import LocalStore
from mobile_auto_mcp.reports.reporter import _report_integrity


def _store_with_run(tmp_path: Path, *, status: str = "pending_review") -> tuple[LocalStore, dict, dict]:
    """Create one isolated session and run without depending on parser or device infrastructure."""
    store = LocalStore(tmp_path)
    rule = store.save_rules(
        [{"case_name": "generic", "api": "/api/profile", "mutations": [{"field": "note", "action": "missing"}]}]
    )[0]
    session = store.start_session("android", [rule["id"]])
    execution_gate = (
        {"change_applied": True, "page_anchor": {"ok": True, "verified": True, "skipped": False}}
        if status == "pending_review"
        else {}
    )
    run = store.record_run_result(
        session["session_id"],
        "android",
        rule["id"],
        status=status,
        execution_gate=execution_gate,
    )
    return store, session, run


def test_visual_precheck_is_stored_without_changing_final_review_status(tmp_path: Path) -> None:
    """验证传统图片算法只提供预检证据，不能把 pending_review 改写成最终 passed/failed。"""
    store, _, run = _store_with_run(tmp_path)

    updated = store.update_run_visual_precheck(run["id"], {"status": "similar", "engine": "pillow_visual_v1"})

    assert updated["status"] == "pending_review"
    assert updated["visual_precheck"]["status"] == "similar"
    assert updated["visual_review"] == {}


def test_session_is_reviewed_only_after_every_run_has_a_final_pass(tmp_path: Path) -> None:
    """验证会话成功态来自全部逐端最终复核，而不是执行完成或内置预检。"""
    store, session, run = _store_with_run(tmp_path)

    awaiting = store.refresh_session_review_status(session["session_id"])
    store.update_manual_review(run["id"], "passed", "人工确认", reviewer="qa")
    reviewed = store.refresh_session_review_status(session["session_id"])

    assert awaiting["status"] == "awaiting_review"
    assert reviewed["status"] == "reviewed"
    assert reviewed["review_complete"] is True


def test_invalid_execution_can_never_be_promoted_to_review_success(tmp_path: Path) -> None:
    """验证执行证据无效时会话保持 blocked，避免后续报告把不可审查结果计为成功。"""
    store, session, _ = _store_with_run(tmp_path, status="invalid_execution")

    result = store.refresh_session_review_status(session["session_id"])

    assert result["status"] == "blocked"
    assert result["review_complete"] is False

    with pytest.raises(ValueError, match="invalid_execution"):
        store.update_manual_review(store.list_runs(session["session_id"])[0]["id"], "passed", "错误提升")


def test_report_integrity_requires_verified_anchor_and_final_review() -> None:
    """验证截图预检或 change_applied 本身不足以让报告完整性门禁通过。"""
    run = {
        "rule_id": "rule-a",
        "target": "android",
        "status": "pending_review",
        "execution_gate": {
            "change_applied": True,
            "page_anchor": {"ok": True, "verified": False, "skipped": True},
        },
        "traceability": {"lane_id": "android", "activation_id": "activation-a"},
    }
    evidence = {"lane_id": "android", "rule_id": "rule-a", "activation_id": "activation-a"}

    result = _report_integrity([run], [{"event": "rule"}], [evidence], [evidence])

    codes = {issue["code"] for issue in result["issues"]}
    assert "page_anchor_unverified" in codes
    assert "final_review_incomplete" in codes


def test_report_integrity_rejects_a_reviewed_run_without_mutation_evidence() -> None:
    """验证手工写入 passed 也不能绕过 change_applied 和关联证据完整性检查。"""
    run = {
        "rule_id": "rule-a",
        "target": "android",
        "status": "passed",
        "execution_gate": {"change_applied": False, "page_anchor": {}},
    }

    result = _report_integrity([run], [{"event": "rule"}], [], [])

    assert result["status"] == "failed"
    assert "response_change_unproven" in {issue["code"] for issue in result["issues"]}


def test_report_integrity_requires_proxy_lifecycle_evidence() -> None:
    """验证报告缺少代理生命周期记录时不能仅凭命中和截图证据返回完整。"""
    run = {
        "rule_id": "rule-a",
        "target": "android",
        "status": "passed",
        "execution_gate": {
            "change_applied": True,
            "page_anchor": {"ok": True, "verified": True, "skipped": False},
        },
        "traceability": {"lane_id": "android", "activation_id": "activation-a"},
    }
    evidence = {"lane_id": "android", "rule_id": "rule-a", "activation_id": "activation-a"}

    result = _report_integrity([run], [{"event": "rule"}], [evidence], [evidence], session={})

    assert "proxy_lifecycle_missing" in {issue["code"] for issue in result["issues"]}
