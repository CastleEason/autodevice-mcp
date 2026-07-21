"""Opt-in physical-device regression matrix for the complete three-platform chain."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from mobile_auto_mcp.execution.preflight import run_preflight
from mobile_auto_mcp.execution.runner import run_cases


pytestmark = pytest.mark.real_device


def _real_config() -> dict[str, Any]:
    """Load explicit operator-owned device and app configuration without committing private identifiers."""
    if os.environ.get("MOBILE_AUTO_MCP_RUN_REAL_DEVICE_TESTS") != "1":
        pytest.skip("set MOBILE_AUTO_MCP_RUN_REAL_DEVICE_TESTS=1 to enable physical-device tests")
    path = Path(os.environ.get("MOBILE_AUTO_MCP_REAL_RUN_CONFIG") or "").expanduser()
    if not path.is_file():
        pytest.skip("set MOBILE_AUTO_MCP_REAL_RUN_CONFIG to a private JSON configuration file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), "real-device configuration must be a JSON object"
    return payload


def test_real_device_readiness_matrix() -> None:
    """Require fresh Android, iOS, and HarmonyOS readiness evidence before a physical chain run."""
    config = _real_config()
    serials = {
        "android": str(config.get("android_serial") or ""),
        "ios": str(config.get("ios_udid") or ""),
        "harmony": str(config.get("harmony_serial") or ""),
    }
    results = {
        target: run_preflight(
            target=target,
            proxy_required=False,
            device_serial=serial,
            wda_url=str(config.get("wda_url") or "") if target == "ios" else "",
        )
        for target, serial in serials.items()
    }

    assert all(result.ok for result in results.values()), {
        target: result.as_dict() for target, result in results.items()
    }


def test_real_triple_chain_reaches_mandatory_review_gate() -> None:
    """Execute the configured three-device chain and reject any false final success before semantic review."""
    config = _real_config()
    arguments = dict(config.get("run_cases") or config)
    arguments["target"] = "triple"

    result = run_cases(**arguments)

    assert result.get("execution_ok") is True, result
    assert result.get("ok") is False, "execution evidence must not bypass final semantic review"
    assert result.get("status") == "awaiting_review"
    lanes = {str((run.get("traceability") or {}).get("lane_id") or run.get("target") or "") for run in result.get("runs") or []}
    assert lanes == {"android", "ios", "harmony"}
