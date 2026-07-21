"""Stable failure codes and recovery playbooks for device execution."""

from __future__ import annotations

from typing import Any


PLAYBOOKS: dict[str, dict[str, Any]] = {
    "stage_timeout": {
        "recoverable": True,
        "next_action": "retry_current_lane_stage",
        "user_action_required": False,
        "remediation": "Retry only the timed-out stage on the affected lane; sibling lanes and sealed cases remain valid.",
    },
    "ios_source_timeout": {
        "recoverable": True,
        "next_action": "fallback_to_visual_locator",
        "user_action_required": False,
        "remediation": "Recreate only the WDA session once, then locate from a fresh screenshot.",
    },
    "ios_runner_unavailable": {
        "recoverable": False,
        "next_action": "restore_wda_runner",
        "user_action_required": True,
        "remediation": "WDA Runner is unavailable. Check the existing signed Runner, device trust, and iproxy; reinstall is not automatic.",
    },
    "harmony_layout_timeout": {
        "recoverable": True,
        "next_action": "reconnect_then_visual_fallback",
        "user_action_required": False,
        "remediation": "Rediscover the current HDC target once, retry layout once, then use screenshot-based location.",
    },
    "harmony_command_timeout": {
        "recoverable": True,
        "next_action": "continue_or_retry_current_worker",
        "user_action_required": False,
        "remediation": "Keep other platform workers running, reconnect the current HDC target, and retry only the interrupted HarmonyOS step.",
    },
    "harmony_device_unavailable": {
        "recoverable": False,
        "next_action": "restore_hdc_target",
        "user_action_required": True,
        "remediation": "No usable HDC target is online. Restore the device connection and debugging authorization.",
    },
    "visual_locator_unavailable": {
        "recoverable": False,
        "next_action": "configure_visual_locator",
        "user_action_required": True,
        "remediation": "Configure a generic screenshot visual locator or provide an element-tree-readable page.",
    },
    "visual_low_confidence": {
        "recoverable": True,
        "next_action": "capture_fresh_screenshot",
        "user_action_required": False,
        "remediation": "Capture a fresh screenshot and locate again; do not click below the configured confidence threshold.",
    },
    "visual_ambiguous": {
        "recoverable": True,
        "next_action": "refine_semantic_locator",
        "user_action_required": False,
        "remediation": "Use additional semantic context or a page anchor to distinguish the competing candidates.",
    },
    "visual_coordinate_out_of_bounds": {
        "recoverable": True,
        "next_action": "reconcile_coordinate_space",
        "user_action_required": False,
        "remediation": "Reconcile screenshot and device coordinate spaces before executing the action.",
    },
    "navigation_drift": {
        "recoverable": True,
        "next_action": "restore_page_anchor",
        "user_action_required": False,
        "remediation": "Restore the declared page precondition before executing this navigation step.",
    },
    "action_not_effective": {
        "recoverable": True,
        "next_action": "relocate_and_retry_once",
        "user_action_required": False,
        "remediation": "Capture fresh evidence, relocate the target, and retry within the action budget.",
    },
}


def build_failure(
    code: str,
    platform: str,
    stage: str,
    evidence: dict[str, Any] | None = None,
    attempts: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Build a machine-readable failure with a stable recovery contract."""
    playbook = PLAYBOOKS.get(
        code,
        {
            "recoverable": False,
            "next_action": "inspect_evidence",
            "user_action_required": True,
            "remediation": "Inspect the attached evidence and restore the failed prerequisite.",
        },
    )
    return {
        "code": code,
        "platform": platform,
        "stage": stage,
        **playbook,
        "evidence": evidence or {},
        "attempts": attempts or [],
        **overrides,
    }
