"""Cross-platform mechanics for inspecting and terminating owned subprocesses."""

from __future__ import annotations

import ntpath
import os
import platform
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable


_WINDOWS_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


@dataclass(frozen=True)
class ProcessIdentity:
    """Describe exact command tokens that distinguish one owned process from a reused PID."""

    executable: str
    required_arguments: tuple[str, ...]

    def matches(self, command: list[str], system: str | None = None) -> bool:
        """Match an executable basename plus one contiguous sequence of exact argv tokens."""
        if not command or not self.executable or not self.required_arguments:
            return False
        host_platform = system or platform.system()
        expected_executable = _normalized_token(_basename(self.executable, host_platform), host_platform)
        expected_arguments = [_normalized_token(token, host_platform) for token in self.required_arguments]
        actual_arguments = [_normalized_token(token, host_platform) for token in command]
        width = len(expected_arguments)
        for executable_index, token in enumerate(command):
            actual_executable = _normalized_token(_basename(token, host_platform), host_platform)
            if actual_executable != expected_executable:
                continue
            if any(
                actual_arguments[index : index + width] == expected_arguments
                for index in range(executable_index + 1, len(actual_arguments) - width + 1)
            ):
                return True
        return False


def popen_session_kwargs(system: str | None = None) -> dict[str, object]:
    """Return detached process-group creation options for the selected host platform."""
    host_platform = system or platform.system()
    if host_platform == "Windows":
        return {"creationflags": _WINDOWS_PROCESS_GROUP}
    if host_platform in {"Darwin", "Linux"}:
        return {"start_new_session": True}
    raise ValueError(f"unsupported host platform: {host_platform}")


def process_command(
    pid: int,
    system: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> list[str]:
    """Inspect one PID and normalize its command line into argv without invoking a shell."""
    if int(pid) <= 0:
        return []
    host_platform = system or platform.system()
    if host_platform == "Windows":
        argv = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f'(Get-CimInstance Win32_Process -Filter "ProcessId = {int(pid)}").CommandLine',
        ]
    elif host_platform in {"Darwin", "Linux"}:
        argv = ["ps", "-p", str(int(pid)), "-o", "command="]
    else:
        raise ValueError(f"unsupported host platform: {host_platform}")
    try:
        completed = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        command_line = str(completed.stdout or "").strip()
        if not command_line:
            return []
        parsed = shlex.split(command_line, posix=host_platform != "Windows")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return [_strip_wrapping_quotes(token) for token in parsed]


def terminate_owned_process(
    pid: int,
    identity: ProcessIdentity,
    system: str | None = None,
) -> dict[str, object]:
    """Gracefully stop an exact owned command, then force only after fresh ownership validation."""
    normalized_pid = int(pid)
    if normalized_pid <= 0:
        return {"ok": False, "status": "invalid_pid", "pid": normalized_pid}
    host_platform = system or platform.system()
    command = process_command(normalized_pid, host_platform)
    if not command:
        return {"ok": True, "status": "already_stopped", "pid": normalized_pid, "forced": False}
    if not identity.matches(command, host_platform):
        return {"ok": False, "status": "ownership_mismatch", "pid": normalized_pid}

    graceful_error = _signal_process_group(normalized_pid, host_platform, force=False)
    if graceful_error is not None:
        return graceful_error
    if _wait_for_exit(normalized_pid, host_platform, timeout=3):
        return {"ok": True, "status": "stopped", "pid": normalized_pid, "forced": False}

    command = process_command(normalized_pid, host_platform)
    if not command:
        return {"ok": True, "status": "stopped", "pid": normalized_pid, "forced": False}
    if not identity.matches(command, host_platform):
        return {"ok": False, "status": "ownership_changed", "pid": normalized_pid}

    force_error = _signal_process_group(normalized_pid, host_platform, force=True)
    if force_error is not None:
        return force_error
    if _wait_for_exit(normalized_pid, host_platform, timeout=1):
        return {"ok": True, "status": "stopped", "pid": normalized_pid, "forced": True}
    return {"ok": False, "status": "still_running", "pid": normalized_pid}


def _signal_process_group(pid: int, system: str, *, force: bool) -> dict[str, object] | None:
    """Send the platform-native graceful or forced process-group termination request."""
    try:
        if system == "Windows":
            argv = ["taskkill", "/PID", str(pid), "/T"]
            if force:
                argv.append("/F")
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if completed.returncode != 0:
                return {
                    "ok": False,
                    "status": "kill_failed" if force else "stop_failed",
                    "pid": pid,
                    "error": str(completed.stderr or completed.stdout or "taskkill failed").strip(),
                }
        else:
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "status": "kill_failed" if force else "stop_failed",
            "pid": pid,
            "error": str(exc),
        }
    return None


def _wait_for_exit(pid: int, system: str, timeout: float) -> bool:
    """Wait a bounded interval for a PID to disappear, reaping direct POSIX children when possible."""
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if system != "Windows":
            try:
                waited_pid, _ = os.waitpid(pid, os.WNOHANG)
                if waited_pid == pid:
                    return True
            except ChildProcessError:
                pass
        if not process_command(pid, system):
            return True
        time.sleep(0.05)
    return not process_command(pid, system)


def _normalized_token(token: str, system: str) -> str:
    """Normalize only host-defined case semantics while preserving exact token boundaries."""
    return token.casefold() if system == "Windows" else token


def _basename(token: str, system: str) -> str:
    """Extract an executable basename using the selected host's path syntax."""
    return ntpath.basename(token) if system == "Windows" else os.path.basename(token)


def _strip_wrapping_quotes(token: str) -> str:
    """Remove one balanced quote pair retained by Windows-compatible shlex parsing."""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        return token[1:-1]
    return token
