"""Cross-platform mechanics for inspecting and terminating owned subprocesses."""

from __future__ import annotations

import ctypes
import errno
import ntpath
import os
import platform
import shlex
import signal
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal


_WINDOWS_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_InspectionStatus = Literal["found", "not_found", "inspection_failed"]


@dataclass(frozen=True)
class ProcessInspection:
    """Carry an argv-preserving process lookup result without conflating absence and failure."""

    status: _InspectionStatus
    command: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class ProcessIdentity:
    """Describe exact argv tokens owned directly or through one explicit interpreter launcher."""

    executable: str
    required_arguments: tuple[str, ...]
    launcher_executables: tuple[str, ...] = ()

    def matches(self, command: list[str] | tuple[str, ...], system: str | None = None) -> bool:
        """Require the program at argv zero or directly behind an explicitly allowed launcher."""
        if not command or not self.executable or not self.required_arguments:
            return False
        host_platform = system or platform.system()
        expected_executable = _normalized_basename(self.executable, host_platform)
        program_index: int | None = None
        if _normalized_basename(command[0], host_platform) == expected_executable:
            program_index = 0
        elif len(command) > 1:
            launchers = {
                _normalized_basename(launcher, host_platform)
                for launcher in self.launcher_executables
            }
            if (
                _normalized_basename(command[0], host_platform) in launchers
                and _normalized_basename(command[1], host_platform) == expected_executable
            ):
                program_index = 1
        if program_index is None:
            return False

        expected_arguments = [_normalized_token(token, host_platform) for token in self.required_arguments]
        actual_arguments = [_normalized_token(token, host_platform) for token in command]
        width = len(expected_arguments)
        return any(
            actual_arguments[index : index + width] == expected_arguments
            for index in range(program_index + 1, len(actual_arguments) - width + 1)
        )


def popen_session_kwargs(system: str | None = None) -> dict[str, object]:
    """Return detached process-group creation options for the selected host platform."""
    host_platform = system or platform.system()
    if host_platform == "Windows":
        return {"creationflags": _WINDOWS_PROCESS_GROUP}
    if host_platform in {"Darwin", "Linux"}:
        return {"start_new_session": True}
    raise ValueError(f"unsupported host platform: {host_platform}")


def inspect_process(
    pid: int,
    system: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> ProcessInspection:
    """Inspect one PID with argv-preserving native facilities and explicit failure states."""
    try:
        normalized_pid = int(pid)
    except (TypeError, ValueError):
        return ProcessInspection(status="not_found")
    if normalized_pid <= 0:
        return ProcessInspection(status="not_found")
    host_platform = system or platform.system()
    if host_platform == "Linux":
        return _inspect_linux(normalized_pid)
    if host_platform == "Darwin":
        return _inspect_darwin(normalized_pid)
    if host_platform == "Windows":
        return _inspect_windows(normalized_pid, runner)
    return ProcessInspection(status="inspection_failed", error=f"unsupported host platform: {host_platform}")


def process_command(
    pid: int,
    system: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> list[str]:
    """Return argv for compatibility while safety-sensitive callers use inspect_process directly."""
    inspection = inspect_process(pid, system, runner)
    return list(inspection.command) if inspection.status == "found" else []


def terminate_owned_process(
    pid: int,
    identity: ProcessIdentity,
    system: str | None = None,
    *,
    process_group: bool = True,
) -> dict[str, object]:
    """Gracefully stop an exact owned command, then force only after fresh ownership validation."""
    normalized_pid = int(pid)
    if normalized_pid <= 0:
        return {"ok": False, "status": "invalid_pid", "pid": normalized_pid}
    host_platform = system or platform.system()
    inspection = inspect_process(normalized_pid, host_platform)
    initial = _validate_inspection(normalized_pid, identity, host_platform, inspection)
    if initial is not None:
        return initial

    graceful_error = _signal_process(
        normalized_pid,
        host_platform,
        force=False,
        process_group=process_group,
    )
    # Windows taskkill can fail for a process that is concurrently exiting or does not accept a
    # non-force close. Its result is therefore resolved by bounded wait and fresh ownership proof.
    if graceful_error is not None and host_platform != "Windows":
        return graceful_error

    waited = _wait_for_exit(normalized_pid, host_platform, timeout=3)
    if waited.status == "not_found":
        return {"ok": True, "status": "stopped", "pid": normalized_pid, "forced": False}
    if waited.status == "inspection_failed":
        return _inspection_failure(normalized_pid, waited)

    # Do not reuse the wait-loop observation for force. A new lookup closes the PID-reuse window
    # immediately before the irreversible escalation action.
    fresh = inspect_process(normalized_pid, host_platform)
    validation = _validate_inspection(normalized_pid, identity, host_platform, fresh, changed=True)
    if validation is not None:
        return validation

    force_error = _signal_process(
        normalized_pid,
        host_platform,
        force=True,
        process_group=process_group,
    )
    if force_error is not None:
        return force_error
    forced_wait = _wait_for_exit(normalized_pid, host_platform, timeout=1)
    if forced_wait.status == "not_found":
        return {"ok": True, "status": "stopped", "pid": normalized_pid, "forced": True}
    if forced_wait.status == "inspection_failed":
        return _inspection_failure(normalized_pid, forced_wait)
    return {"ok": False, "status": "still_running", "pid": normalized_pid}


def _inspect_linux(pid: int) -> ProcessInspection:
    """Read Linux procfs cmdline bytes so spaces never erase argv boundaries."""
    try:
        raw = _read_linux_command(pid)
    except FileNotFoundError:
        return ProcessInspection(status="not_found")
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ESRCH}:
            return ProcessInspection(status="not_found")
        return ProcessInspection(status="inspection_failed", error=str(exc))
    if not raw:
        return ProcessInspection(status="inspection_failed", error="process cmdline is empty")
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    command = tuple(os.fsdecode(field) for field in fields)
    if not command or not command[0]:
        return ProcessInspection(status="inspection_failed", error="process argv zero is unavailable")
    return ProcessInspection(status="found", command=command)


def _read_linux_command(pid: int) -> bytes:
    """Read one Linux process's native null-delimited argv payload."""
    return Path(f"/proc/{pid}/cmdline").read_bytes()


def _inspect_darwin(pid: int) -> ProcessInspection:
    """Read macOS KERN_PROCARGS2 argv and retain native token boundaries."""
    try:
        command = tuple(_read_darwin_command(pid))
    except OSError as exc:
        # KERN_PROCARGS2 reports EINVAL for a PID that no longer exists on supported macOS releases.
        if exc.errno in {errno.EINVAL, errno.ENOENT, errno.ESRCH}:
            return ProcessInspection(status="not_found")
        return ProcessInspection(status="inspection_failed", error=str(exc))
    except (IndexError, struct.error, ValueError) as exc:
        return ProcessInspection(status="inspection_failed", error=str(exc))
    if not command or not command[0]:
        return ProcessInspection(status="inspection_failed", error="process argv zero is unavailable")
    return ProcessInspection(status="found", command=command)


def _read_darwin_command(pid: int) -> tuple[str, ...]:
    """Decode macOS KERN_PROCARGS2 into the process's original argv tuple."""
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, PID
    size = ctypes.c_size_t()
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    raw = buffer.raw[: size.value]
    argument_count = struct.unpack_from("=i", raw, 0)[0]
    cursor = struct.calcsize("=i")
    executable_end = raw.index(b"\0", cursor)
    cursor = executable_end + 1
    while cursor < len(raw) and raw[cursor] == 0:
        cursor += 1
    arguments: list[str] = []
    for _ in range(argument_count):
        argument_end = raw.index(b"\0", cursor)
        arguments.append(os.fsdecode(raw[cursor:argument_end]))
        cursor = argument_end + 1
    return tuple(arguments)


def _inspect_windows(pid: int, runner: Callable[..., Any]) -> ProcessInspection:
    """Query Windows CIM and parse its native command line with CommandLineToArgvW."""
    argv = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            f'$process = Get-CimInstance Win32_Process -Filter "ProcessId = {pid}" -ErrorAction Stop; '
            "if ($null -eq $process) { exit 3 }; [Console]::Out.Write($process.CommandLine)"
        ),
    ]
    try:
        completed = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ProcessInspection(status="inspection_failed", error=str(exc))
    if completed.returncode == 3:
        return ProcessInspection(status="not_found")
    if completed.returncode != 0:
        error = str(completed.stderr or completed.stdout or "PowerShell process inspection failed").strip()
        return ProcessInspection(status="inspection_failed", error=error)
    command_line = str(completed.stdout or "").strip()
    if not command_line:
        return ProcessInspection(status="inspection_failed", error="Windows CommandLine is empty")
    try:
        command = tuple(_split_windows_command_line(command_line))
    except (OSError, ValueError) as exc:
        return ProcessInspection(status="inspection_failed", error=str(exc))
    if not command or not command[0]:
        return ProcessInspection(status="inspection_failed", error="process argv zero is unavailable")
    return ProcessInspection(status="found", command=command)


def _split_windows_command_line(command_line: str) -> list[str]:
    """Use CommandLineToArgvW on Windows and a deterministic compatibility parser in tests."""
    if os.name != "nt":
        return [_strip_wrapping_quotes(token) for token in shlex.split(command_line, posix=False)]
    argument_count = ctypes.c_int()
    command_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_to_argv.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    command_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    pointer = command_to_argv(command_line, ctypes.byref(argument_count))
    if not pointer:
        error_number = ctypes.get_last_error()
        raise OSError(error_number, os.strerror(error_number))
    try:
        return [pointer[index] for index in range(argument_count.value)]
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(pointer)


def _validate_inspection(
    pid: int,
    identity: ProcessIdentity,
    system: str,
    inspection: ProcessInspection,
    *,
    changed: bool = False,
) -> dict[str, object] | None:
    """Convert inspection state and exact identity matching into a termination gate result."""
    if inspection.status == "not_found":
        status = "stopped" if changed else "already_stopped"
        return {"ok": True, "status": status, "pid": pid, "forced": False}
    if inspection.status == "inspection_failed":
        return _inspection_failure(pid, inspection)
    if not identity.matches(inspection.command, system):
        return {"ok": False, "status": "ownership_changed" if changed else "ownership_mismatch", "pid": pid}
    return None


def _inspection_failure(pid: int, inspection: ProcessInspection) -> dict[str, object]:
    """Return a stable failure that tells callers to preserve ownership evidence."""
    return {
        "ok": False,
        "status": "inspection_failed",
        "pid": pid,
        "error": inspection.error or "process inspection failed",
    }


def _signal_process(
    pid: int,
    system: str,
    *,
    force: bool,
    process_group: bool,
) -> dict[str, object] | None:
    """Send the platform-native graceful or forced request to the verified target scope."""
    try:
        if system == "Windows":
            argv = ["taskkill", "/PID", str(pid)]
            if process_group:
                argv.append("/T")
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
        elif process_group:
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
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


def _wait_for_exit(pid: int, system: str, timeout: float) -> ProcessInspection:
    """Wait a bounded interval, returning explicit absence, presence, or inspection failure."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if system != "Windows":
            try:
                waited_pid, _ = os.waitpid(pid, os.WNOHANG)
                if waited_pid == pid:
                    return ProcessInspection(status="not_found")
            except ChildProcessError:
                pass
        inspection = inspect_process(pid, system)
        if inspection.status != "found" or time.monotonic() >= deadline:
            return inspection
        time.sleep(0.05)


def _normalized_token(token: str, system: str) -> str:
    """Normalize only host-defined case semantics while preserving exact token boundaries."""
    return token.casefold() if system == "Windows" else token


def _normalized_basename(token: str, system: str) -> str:
    """Extract and normalize one executable basename with the selected host's path syntax."""
    basename = ntpath.basename(token) if system == "Windows" else os.path.basename(token)
    return _normalized_token(basename, system)


def _strip_wrapping_quotes(token: str) -> str:
    """Remove one balanced quote pair retained by Windows-compatible shlex parsing."""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        return token[1:-1]
    return token
