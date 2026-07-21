"""Tests for host capability gating at the device-readiness boundary."""

from __future__ import annotations

import subprocess
import sys

import pytest

from mobile_auto_mcp.execution import preflight
from mobile_auto_mcp.platform import capabilities
from mobile_auto_mcp.platform.capabilities import host_capability


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_ios_is_a_structured_non_macos_capability_failure(system: str) -> None:
    """Expose iOS as unavailable on non-macOS hosts without disabling the MCP process."""
    assert host_capability("ios", system) == {
        "ok": False,
        "code": "platform_not_supported",
        "target": "ios",
        "host_platform": system,
    }


@pytest.mark.parametrize("target", ["android", "harmony"])
def test_android_and_harmony_remain_available_on_windows(target: str) -> None:
    """Keep non-iOS device lanes available when the host is Windows."""
    assert host_capability(target, "Windows") == {
        "ok": True,
        "code": "platform_supported",
        "target": target,
        "host_platform": "Windows",
    }


def test_non_macos_ios_preflight_returns_before_any_wda_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return a normal structured blocker before WDA status or startup logic can run."""
    monkeypatch.setattr(capabilities.platform, "system", lambda: "Windows")

    def unexpected_wda_call(*args: object, **kwargs: object) -> object:
        """Fail if the unsupported host crosses into any iOS-only readiness operation."""
        raise AssertionError(f"WDA must not run on Windows: {args!r} {kwargs!r}")

    monkeypatch.setattr(preflight, "_wda_status", unexpected_wda_call)

    result = preflight.run_preflight(target="ios", proxy_required=False, auto_start_wda=True)

    assert result.ok is False
    assert result.failures[0]["code"] == "platform_not_supported"
    assert result.checks["host_capability"] == {
        "ok": False,
        "code": "platform_not_supported",
        "target": "ios",
        "host_platform": "Windows",
    }
    assert result.phone_proxy_policy == "detect_and_prompt_only"
    assert "wda" not in result.checks


def test_non_macos_ios_preflight_does_not_import_wda_modules() -> None:
    """Keep unsupported-host readiness importable when WDA-only modules are unavailable."""
    script = """
import importlib.abc
import platform
import sys

class BlockWDAImports(importlib.abc.MetaPathFinder):
    \"\"\"Reject WDA modules to prove the capability gate precedes their import.\"\"\"

    def find_spec(self, fullname, path=None, target=None):
        \"\"\"Raise if readiness attempts to import an iOS adapter or guardian.\"\"\"
        if fullname in {
            'mobile_auto_mcp.execution.adapters.ios',
            'mobile_auto_mcp.execution.wda_guardian',
        }:
            raise ModuleNotFoundError(fullname)
        return None

platform.system = lambda: 'Windows'
sys.meta_path.insert(0, BlockWDAImports())
from mobile_auto_mcp.execution.preflight import run_preflight
result = run_preflight(target='ios', proxy_required=False, auto_start_wda=True)
assert result.failures[0]['code'] == 'platform_not_supported'
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
