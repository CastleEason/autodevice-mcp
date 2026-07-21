"""Cross-platform process creation, inspection, and owned-termination contracts."""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mobile_auto_mcp.platform import processes
from mobile_auto_mcp.platform.processes import (
    ProcessIdentity,
    popen_session_kwargs,
    process_command,
    terminate_owned_process,
)
from mobile_auto_mcp.proxy import proxy_manager as proxy_manager_module
from mobile_auto_mcp.proxy.proxy_manager import ProxyManager
from mobile_auto_mcp.reports import server as report_server_module
from mobile_auto_mcp.reports.server import ReportServerManager


def test_popen_session_kwargs_are_platform_specific() -> None:
    """Create an independent POSIX session or Windows process group without shell flags."""
    assert popen_session_kwargs("Linux") == {"start_new_session": True}
    assert popen_session_kwargs("Darwin") == {"start_new_session": True}
    assert popen_session_kwargs("Windows") == {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200),
    }


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_process_command_uses_ps_on_posix(system: str) -> None:
    """Inspect a POSIX PID with ps and normalize its shell-quoted command into exact argv."""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        """Record the shell-free process inspection and return one path containing spaces."""
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="mitmdump -s '/tmp/addon dir/proxy_addon.py'\n")

    assert process_command(4242, system, runner=runner) == [
        "mitmdump",
        "-s",
        "/tmp/addon dir/proxy_addon.py",
    ]
    assert calls == [
        (
            ["ps", "-p", "4242", "-o", "command="],
            {"capture_output": True, "text": True, "timeout": 2, "check": False},
        )
    ]


def test_process_command_uses_powershell_cim_on_windows() -> None:
    """Inspect Windows CommandLine through CIM and normalize quoted arguments into argv."""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        """Record the PowerShell query and return a quoted Windows command line."""
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout='"C:\\Python312\\python.exe" -m http.server 13080 --directory "C:\\QA Reports"\n',
        )

    assert process_command(4242, "Windows", runner=runner) == [
        "C:\\Python312\\python.exe",
        "-m",
        "http.server",
        "13080",
        "--directory",
        "C:\\QA Reports",
    ]
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    assert "Get-CimInstance Win32_Process" in argv[4]
    assert "ProcessId = 4242" in argv[4]
    assert kwargs == {"capture_output": True, "text": True, "timeout": 2, "check": False}


@pytest.mark.parametrize(
    ("system", "identity", "observed"),
    [
        (
            "Linux",
            ProcessIdentity("mitmdump", ("-s", "/owned/proxy_addon.py")),
            ["mitmdump", "-s", "/owned/proxy_addon.py.old"],
        ),
        (
            "Windows",
            ProcessIdentity("python.exe", ("--directory", "C:\\owned\\reports")),
            ["C:\\Python312\\python.exe", "-m", "http.server", "--directory", "C:\\owned\\reports-old"],
        ),
    ],
)
def test_termination_never_signals_a_command_with_only_a_partial_identity_match(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    identity: ProcessIdentity,
    observed: list[str],
) -> None:
    """Reject addon/report-root prefixes so PID reuse can never target an unrelated process."""
    signals: list[tuple[int, signal.Signals]] = []
    commands: list[list[str]] = []
    monkeypatch.setattr(processes, "process_command", lambda pid, system=None: observed)
    monkeypatch.setattr(processes.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(
        processes.subprocess,
        "run",
        lambda argv, **kwargs: commands.append(argv) or SimpleNamespace(returncode=0, stdout=""),
    )

    result = terminate_owned_process(4242, identity, system)

    assert result == {"ok": False, "status": "ownership_mismatch", "pid": 4242}
    assert signals == []
    assert commands == []


def test_identity_requires_executable_marker_before_owned_arguments() -> None:
    """Reject unrelated commands that mention the expected executable only after owned argv."""
    identity = ProcessIdentity("mitmdump", ("-s", "/owned/proxy_addon.py"))

    assert identity.matches(["unrelated", "-s", "/owned/proxy_addon.py", "mitmdump"], "Linux") is False


def test_posix_termination_revalidates_ownership_before_forcing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Send SIGKILL to a process group only while its exact identity still matches."""
    identity = ProcessIdentity("mitmdump", ("-s", "/owned/proxy_addon.py"))
    inspections = [
        ["/venv/bin/mitmdump", "-s", "/owned/proxy_addon.py"],
        ["/venv/bin/mitmdump", "-s", "/owned/proxy_addon.py"],
    ]
    signals: list[tuple[int, signal.Signals]] = []
    waits = iter([False, True])
    monkeypatch.setattr(processes, "process_command", lambda pid, system=None: inspections.pop(0))
    monkeypatch.setattr(processes.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(processes, "_wait_for_exit", lambda pid, system, timeout: next(waits))

    result = terminate_owned_process(4242, identity, "Linux")

    assert result == {"ok": True, "status": "stopped", "pid": 4242, "forced": True}
    assert signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


def test_windows_termination_uses_taskkill_and_adds_force_only_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use taskkill for a verified Windows tree and reserve /F for bounded escalation."""
    identity = ProcessIdentity("python.exe", ("--directory", "C:\\owned\\reports"))
    observed = ["C:\\Python312\\python.exe", "--directory", "C:\\owned\\reports"]
    commands: list[list[str]] = []
    waits = iter([False, True])
    monkeypatch.setattr(processes, "process_command", lambda pid, system=None: observed)
    monkeypatch.setattr(processes, "_wait_for_exit", lambda pid, system, timeout: next(waits))
    monkeypatch.setattr(
        processes.subprocess,
        "run",
        lambda argv, **kwargs: commands.append(argv) or SimpleNamespace(returncode=0, stdout=""),
    )

    result = terminate_owned_process(4242, identity, "Windows")

    assert result == {"ok": True, "status": "stopped", "pid": 4242, "forced": True}
    assert commands == [
        ["taskkill", "/PID", "4242", "/T"],
        ["taskkill", "/PID", "4242", "/T", "/F"],
    ]


def test_force_is_skipped_when_ownership_changes_during_graceful_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve a reused PID when its command changes after the graceful signal."""
    identity = ProcessIdentity("mitmdump", ("-s", "/owned/proxy_addon.py"))
    inspections = [
        ["mitmdump", "-s", "/owned/proxy_addon.py"],
        ["unrelated", "/owned/proxy_addon.py"],
    ]
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(processes, "process_command", lambda pid, system=None: inspections.pop(0))
    monkeypatch.setattr(processes.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(processes, "_wait_for_exit", lambda pid, system, timeout: False)

    result = terminate_owned_process(4242, identity, "Darwin")

    assert result == {"ok": False, "status": "ownership_changed", "pid": 4242}
    assert signals == [(4242, signal.SIGTERM)]


@pytest.mark.parametrize("manager_kind", ["proxy", "report"])
def test_managed_servers_start_in_an_independent_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager_kind: str,
) -> None:
    """Route both long-lived server launches through the shared session adapter."""
    popen_calls: list[dict[str, Any]] = []

    class FakeProcess:
        """Emulate a running child long enough for each manager to persist ownership."""

        pid = 4242

        def poll(self) -> None:
            """Keep the fake process alive during its manager's readiness loop."""
            return None

    def fake_popen(argv: list[str], **kwargs: Any) -> FakeProcess:
        """Capture process creation options without launching a real server."""
        popen_calls.append(kwargs)
        return FakeProcess()

    if manager_kind == "proxy":
        manager = ProxyManager(tmp_path, target="android", port=13000)
        readiness = iter([False, True])
        monkeypatch.setattr(proxy_manager_module, "_is_port_open", lambda port: next(readiness))
        monkeypatch.setattr(proxy_manager_module.subprocess, "Popen", fake_popen)
        try:
            result = manager.start()
        finally:
            for handle in (manager._stdout, manager._stderr):
                if handle:
                    handle.close()
    else:
        manager = ReportServerManager(tmp_path, port=13080)
        readiness = iter([False, True])
        monkeypatch.setattr(manager, "status", lambda: {"running": False})
        monkeypatch.setattr(manager, "_port_ready", lambda port=None: next(readiness))
        monkeypatch.setattr(report_server_module.subprocess, "Popen", fake_popen)
        result = manager.start()

    assert result["ok"] is True
    assert len(popen_calls) == 1
    assert popen_calls[0]["start_new_session"] is True


def test_proxy_retained_stop_delegates_exact_addon_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep static proxy evidence in the caller and delegate only verified process mechanics."""
    manager = ProxyManager(tmp_path, target="android", port=13000)
    runtime = {
        "pid": 4242,
        "port": 13000,
        "home": str(tmp_path.resolve()),
        "addon": str(manager.addon_path.resolve()),
    }
    delegated: list[tuple[int, ProcessIdentity]] = []
    monkeypatch.setattr(proxy_manager_module, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(proxy_manager_module, "_pid_matches_runtime", lambda pid, addon: True)
    monkeypatch.setattr(
        proxy_manager_module,
        "terminate_owned_process",
        lambda pid, identity: delegated.append((pid, identity))
        or {"ok": True, "status": "stopped", "pid": pid, "forced": False},
        raising=False,
    )

    result = manager.stop_owned_retained(runtime)

    assert result["ok"] is True
    assert delegated == [
        (4242, ProcessIdentity("mitmdump", ("-s", str(manager.addon_path.resolve())))),
    ]


def test_report_stop_delegates_exact_root_identity_and_keeps_failed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep report ownership evidence when the shared adapter cannot stop its exact command."""
    manager = ReportServerManager(tmp_path, host="127.0.0.1", port=13080)
    state = {
        "pid": 4242,
        "host": manager.host,
        "port": manager.port,
        "report_root": str(manager.report_root),
        "started_at": 1.0,
    }
    manager._write_state(state)
    observed = [
        sys.executable,
        "-m",
        "http.server",
        "13080",
        "--bind",
        "127.0.0.1",
        "--directory",
        str(manager.report_root),
    ]
    delegated: list[tuple[int, ProcessIdentity]] = []
    monkeypatch.setattr(report_server_module, "process_command", lambda pid: observed)
    monkeypatch.setattr(
        report_server_module,
        "terminate_owned_process",
        lambda pid, identity: delegated.append((pid, identity))
        or {"ok": False, "status": "still_running", "pid": pid},
    )

    result = manager.stop()

    assert result["stopped"] is False
    assert delegated == [
        (
            4242,
            ProcessIdentity(
                Path(sys.executable).name,
                (
                    "-m",
                    "http.server",
                    "13080",
                    "--bind",
                    "127.0.0.1",
                    "--directory",
                    str(manager.report_root),
                ),
            ),
        ),
    ]
    assert manager._read_state() == state
