"""Tests for platform-specific, shell-free host network probes."""

from __future__ import annotations

from typing import Any

import pytest

from mobile_auto_mcp.platform import network
from mobile_auto_mcp.platform.network import ping_command
from mobile_auto_mcp.proxy.host_selection import probe_proxy_host_reachability


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        ("Darwin", ["/sbin/ping", "-S", "10.0.0.2", "-c", "1", "-W", "1000", "10.0.0.3"]),
        ("Linux", ["ping", "-I", "10.0.0.2", "-c", "1", "-W", "1", "10.0.0.3"]),
        ("Windows", ["ping", "-S", "10.0.0.2", "-n", "1", "-w", "1000", "10.0.0.3"]),
    ],
)
def test_ping_commands_are_platform_specific(system: str, expected: list[str]) -> None:
    """Build the exact source-bound argv required by each supported desktop host."""
    assert ping_command("10.0.0.2", "10.0.0.3", system) == expected


def test_ping_command_rejects_an_unknown_host_platform() -> None:
    """Reject unknown command syntax instead of silently running an incorrect platform probe."""
    with pytest.raises(ValueError, match="unsupported host platform"):
        ping_command("10.0.0.2", "10.0.0.3", "Plan9")


def test_reachability_probe_uses_platform_command_as_an_argv_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the production route probe consumes the adapter without invoking a command shell."""
    commands: list[list[str]] = []
    monkeypatch.setattr(network.platform, "system", lambda: "Linux")

    def runner(command: list[str]) -> Any:
        """Capture the command passed directly to the injected subprocess-compatible runner."""
        commands.append(command)
        return type("Result", (), {"returncode": 0})()

    result = probe_proxy_host_reachability(
        "10.0.0.2",
        {"android": "10.0.0.3"},
        command_runner=runner,
    )

    assert result["ok"] is True
    assert commands == [["ping", "-I", "10.0.0.2", "-c", "1", "-W", "1", "10.0.0.3"]]
