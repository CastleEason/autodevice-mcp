"""Cross-platform process creation, inspection, and owned-termination contracts."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mobile_auto_mcp.platform import processes
from mobile_auto_mcp.platform.processes import (
    ProcessIdentity,
    ProcessInspection,
    inspect_process,
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


def test_linux_process_command_reads_null_delimited_proc_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve spaces and token boundaries by reading Linux procfs argv bytes directly."""
    monkeypatch.setattr(
        processes,
        "_read_linux_command",
        lambda pid: b"/usr/bin/python3\0/tmp/script dir/worker.py\0argument with spaces\0",
        raising=False,
    )

    inspection = inspect_process(4242, "Linux")

    assert inspection == ProcessInspection(
        status="found",
        command=("/usr/bin/python3", "/tmp/script dir/worker.py", "argument with spaces"),
    )
    assert process_command(4242, "Linux") == list(inspection.command)


def test_darwin_process_command_uses_native_argv_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the native KERN_PROCARGS2 reader without reconstructing a display command."""
    expected = ("/usr/bin/python3", "/tmp/script dir/worker.py", "argument with spaces")
    monkeypatch.setattr(processes, "_read_darwin_command", lambda pid: expected, raising=False)

    assert inspect_process(4242, "Darwin") == ProcessInspection(status="found", command=expected)


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
    ("runner", "expected_status"),
    [
        (lambda argv, **kwargs: SimpleNamespace(returncode=3, stdout="", stderr=""), "not_found"),
        (lambda argv, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="Access denied"), "inspection_failed"),
        (
            lambda argv, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 2)),
            "inspection_failed",
        ),
    ],
)
def test_windows_inspection_distinguishes_absence_from_tool_failure(
    runner: Any,
    expected_status: str,
) -> None:
    """Treat only PowerShell's explicit not-found code as confirmed process absence."""
    inspection = inspect_process(4242, "Windows", runner=runner)

    assert inspection.status == expected_status
    assert inspection.command == ()


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
    monkeypatch.setattr(
        processes,
        "inspect_process",
        lambda pid, system=None: ProcessInspection(status="found", command=tuple(observed)),
    )
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
    """Reject echo-style marker arguments while permitting an explicit interpreter chain."""
    identity = ProcessIdentity(
        "mitmdump",
        ("-s", "/owned/proxy_addon.py"),
        launcher_executables=("python3",),
    )

    assert identity.matches(["unrelated", "-s", "/owned/proxy_addon.py", "mitmdump"], "Linux") is False
    assert identity.matches(["echo", "mitmdump", "-s", "/owned/proxy_addon.py"], "Linux") is False
    assert identity.matches(["python3", "/venv/bin/mitmdump", "-s", "/owned/proxy_addon.py"], "Linux") is True


def test_posix_termination_revalidates_ownership_before_forcing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Send SIGKILL to a process group only while its exact identity still matches."""
    identity = ProcessIdentity("mitmdump", ("-s", "/owned/proxy_addon.py"))
    command = ("/venv/bin/mitmdump", "-s", "/owned/proxy_addon.py")
    inspections = [ProcessInspection(status="found", command=command)] * 2
    signals: list[tuple[int, signal.Signals]] = []
    waits = iter(
        [
            ProcessInspection(status="found", command=command),
            ProcessInspection(status="not_found"),
        ]
    )
    monkeypatch.setattr(processes, "inspect_process", lambda pid, system=None: inspections.pop(0))
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
    observed = ("C:\\Python312\\python.exe", "--directory", "C:\\owned\\reports")
    commands: list[list[str]] = []
    waits = iter(
        [
            ProcessInspection(status="found", command=observed),
            ProcessInspection(status="not_found"),
        ]
    )
    monkeypatch.setattr(
        processes,
        "inspect_process",
        lambda pid, system=None: ProcessInspection(status="found", command=observed),
    )
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


def test_windows_graceful_taskkill_failure_still_revalidates_before_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wait and re-inspect after non-force taskkill failure before issuing /F."""
    identity = ProcessIdentity("python.exe", ("--directory", "C:\\owned\\reports"))
    observed = ("C:\\Python312\\python.exe", "--directory", "C:\\owned\\reports")
    inspections = iter(
        [
            ProcessInspection(status="found", command=observed),
            ProcessInspection(status="found", command=observed),
        ]
    )
    waits = iter(
        [
            ProcessInspection(status="found", command=observed),
            ProcessInspection(status="not_found"),
        ]
    )
    commands: list[list[str]] = []
    returncodes = iter([1, 0])
    monkeypatch.setattr(processes, "inspect_process", lambda pid, system=None: next(inspections))
    monkeypatch.setattr(processes, "_wait_for_exit", lambda pid, system, timeout: next(waits))
    monkeypatch.setattr(
        processes.subprocess,
        "run",
        lambda argv, **kwargs: commands.append(argv)
        or SimpleNamespace(returncode=next(returncodes), stdout="", stderr="failed"),
    )

    result = terminate_owned_process(4242, identity, "Windows")

    assert result == {"ok": True, "status": "stopped", "pid": 4242, "forced": True}
    assert commands == [
        ["taskkill", "/PID", "4242", "/T"],
        ["taskkill", "/PID", "4242", "/T", "/F"],
    ]


def test_windows_graceful_failure_does_not_force_a_vanished_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classify confirmed absence after failed taskkill as stopped without issuing /F."""
    identity = ProcessIdentity("python.exe", ("--directory", "C:\\owned\\reports"))
    observed = ("C:\\Python312\\python.exe", "--directory", "C:\\owned\\reports")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        processes,
        "inspect_process",
        lambda pid, system=None: ProcessInspection(status="found", command=observed),
    )
    monkeypatch.setattr(
        processes,
        "_wait_for_exit",
        lambda pid, system, timeout: ProcessInspection(status="not_found"),
    )
    monkeypatch.setattr(
        processes.subprocess,
        "run",
        lambda argv, **kwargs: commands.append(argv)
        or SimpleNamespace(returncode=1, stdout="", stderr="not found"),
    )

    result = terminate_owned_process(4242, identity, "Windows")

    assert result == {"ok": True, "status": "stopped", "pid": 4242, "forced": False}
    assert commands == [["taskkill", "/PID", "4242", "/T"]]


def test_force_is_skipped_when_ownership_changes_during_graceful_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve a reused PID when its command changes after the graceful signal."""
    identity = ProcessIdentity("mitmdump", ("-s", "/owned/proxy_addon.py"))
    inspections = [
        ProcessInspection(status="found", command=("mitmdump", "-s", "/owned/proxy_addon.py")),
        ProcessInspection(status="found", command=("unrelated", "/owned/proxy_addon.py")),
    ]
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(processes, "inspect_process", lambda pid, system=None: inspections.pop(0))
    monkeypatch.setattr(processes.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(
        processes,
        "_wait_for_exit",
        lambda pid, system, timeout: ProcessInspection(
            status="found",
            command=("mitmdump", "-s", "/owned/proxy_addon.py"),
        ),
    )

    result = terminate_owned_process(4242, identity, "Darwin")

    assert result == {"ok": False, "status": "ownership_changed", "pid": 4242}
    assert signals == [(4242, signal.SIGTERM)]


def test_inspection_failure_after_graceful_signal_prevents_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve ownership evidence and skip force when re-inspection cannot prove identity."""
    identity = ProcessIdentity("mitmdump", ("-s", "/owned/proxy_addon.py"))
    command = ("mitmdump", "-s", "/owned/proxy_addon.py")
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        processes,
        "inspect_process",
        lambda pid, system=None: ProcessInspection(status="found", command=command),
    )
    monkeypatch.setattr(processes.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(
        processes,
        "_wait_for_exit",
        lambda pid, system, timeout: ProcessInspection(status="inspection_failed", error="permission denied"),
    )

    result = terminate_owned_process(4242, identity, "Linux")

    assert result == {
        "ok": False,
        "status": "inspection_failed",
        "pid": 4242,
        "error": "permission denied",
    }
    assert signals == [(4242, signal.SIGTERM)]


def test_legacy_posix_process_uses_single_pid_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid killpg for a verified retained process created before session evidence existed."""
    identity = ProcessIdentity("mitmdump", ("-s", "/owned/proxy_addon.py"))
    command = ("mitmdump", "-s", "/owned/proxy_addon.py")
    pid_signals: list[tuple[int, signal.Signals]] = []
    group_signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        processes,
        "inspect_process",
        lambda pid, system=None: ProcessInspection(status="found", command=command),
    )
    monkeypatch.setattr(
        processes,
        "_wait_for_exit",
        lambda pid, system, timeout: ProcessInspection(status="not_found"),
    )
    monkeypatch.setattr(processes.os, "kill", lambda pid, sig: pid_signals.append((pid, sig)))
    monkeypatch.setattr(processes.os, "killpg", lambda pid, sig: group_signals.append((pid, sig)))

    result = terminate_owned_process(4242, identity, "Linux", process_group=False)

    assert result["ok"] is True
    assert pid_signals == [(4242, signal.SIGTERM)]
    assert group_signals == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group integration")
def test_real_owned_subprocess_with_spaced_argv_is_inspected_and_stopped(tmp_path: Path) -> None:
    """Prove native argv inspection and termination against only a test-owned subprocess."""
    script_dir = tmp_path / "owned process dir"
    script_dir.mkdir()
    script_path = script_dir / "sleep worker.py"
    script_path.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    marker = "argument with spaces"
    child = subprocess.Popen(
        [sys.executable, str(script_path), marker],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **popen_session_kwargs(),
    )
    try:
        deadline = time.monotonic() + 3
        inspection = ProcessInspection(status="inspection_failed", error="not inspected")
        while time.monotonic() < deadline:
            inspection = inspect_process(child.pid)
            if inspection.status == "found":
                break
            time.sleep(0.02)

        assert inspection.status == "found"
        assert str(script_path) in inspection.command
        assert marker in inspection.command
        identity = ProcessIdentity(Path(sys.executable).name, (str(script_path), marker))

        result = terminate_owned_process(child.pid, identity, process_group=True)

        assert result["ok"] is True
        assert result["status"] == "stopped"
        assert child.wait(timeout=3) is not None
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=3)


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
    if manager_kind == "proxy":
        assert manager.runtime_evidence()["process_group"] is True
    else:
        assert manager._read_state()["process_group"] is True


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
    command = ("mitmdump", "-s", str(manager.addon_path.resolve()))
    delegated: list[tuple[int, ProcessIdentity, bool]] = []
    monkeypatch.setattr(
        proxy_manager_module,
        "inspect_process",
        lambda pid: ProcessInspection(status="found", command=command),
        raising=False,
    )
    monkeypatch.setattr(
        proxy_manager_module,
        "terminate_owned_process",
        lambda pid, identity, *, process_group: delegated.append((pid, identity, process_group))
        or {"ok": True, "status": "stopped", "pid": pid, "forced": False},
    )

    result = manager.stop_owned_retained(runtime)

    assert result["ok"] is True
    assert delegated == [
        (
            4242,
            ProcessIdentity(
                "mitmdump",
                ("-s", str(manager.addon_path.resolve())),
                launcher_executables=(Path(sys.executable).name,),
            ),
            False,
        ),
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
    delegated: list[tuple[int, ProcessIdentity, bool]] = []
    monkeypatch.setattr(
        report_server_module,
        "inspect_process",
        lambda pid: ProcessInspection(status="found", command=tuple(observed)),
        raising=False,
    )
    monkeypatch.setattr(
        report_server_module,
        "terminate_owned_process",
        lambda pid, identity, *, process_group: delegated.append((pid, identity, process_group))
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
            False,
        ),
    ]
    assert manager._read_state() == state


@pytest.mark.parametrize("manager_kind", ["proxy", "report"])
def test_inspection_failure_preserves_owned_runtime_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager_kind: str,
) -> None:
    """Never erase proxy or report ownership evidence when command inspection fails."""
    failed = ProcessInspection(status="inspection_failed", error="permission denied")
    if manager_kind == "proxy":
        manager = ProxyManager(tmp_path, target="android", port=13000)
        state = {
            "pid": 4242,
            "port": manager.port,
            "home": str(tmp_path.resolve()),
            "addon": str(manager.addon_path.resolve()),
            "process_group": True,
        }
        manager.runtime_path.parent.mkdir(parents=True, exist_ok=True)
        manager.runtime_path.write_text(json.dumps(state), encoding="utf-8")
        monkeypatch.setattr(proxy_manager_module, "inspect_process", lambda pid: failed, raising=False)
        result = manager.stop_owned_retained(state)
        evidence_path = manager.runtime_path
    else:
        manager = ReportServerManager(tmp_path, port=13080)
        state = {
            "pid": 4242,
            "host": manager.host,
            "port": manager.port,
            "report_root": str(manager.report_root),
            "process_group": True,
        }
        manager._write_state(state)
        monkeypatch.setattr(report_server_module, "inspect_process", lambda pid: failed, raising=False)
        result = manager.stop()
        evidence_path = manager.state_path

    assert result.get("ok") is False or result.get("stopped") is False
    assert evidence_path.exists()
