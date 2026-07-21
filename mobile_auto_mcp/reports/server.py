"""Persistent local HTTP service for generated report hubs."""

from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mobile_auto_mcp.platform.processes import (
    ProcessIdentity,
    ProcessInspection,
    inspect_process,
    popen_session_kwargs,
    terminate_owned_process,
    trusted_python_executables,
)
from mobile_auto_mcp.state.private_files import atomic_write_private_text, ensure_private_directory


class ReportServerManager:
    """Start, inspect, and stop a report-only HTTP server without app coupling."""

    # LAN sharing is opt-in because reports can contain sensitive test evidence.
    def __init__(self, report_root: str | Path, *, host: str = "127.0.0.1", port: int = 13080) -> None:
        """Bind server configuration to one report root and persist lifecycle metadata there."""
        self.report_root = Path(report_root).expanduser().resolve()
        self.host = host or "127.0.0.1"
        self.port = int(port)
        self.state_path = self.report_root / ".report_server.json"
        self.log_path = self.report_root / "report_server.log"

    def start(self) -> dict[str, Any]:
        """Start the server when absent and return stable local/LAN access URLs."""
        ensure_private_directory(self.report_root)
        current = self.status()
        if current.get("running"):
            return {"ok": True, "already_running": True, **current}
        if self._port_ready():
            return {"ok": False, **current, "error": f"端口 {self.port} 已被其他进程占用"}

        # 使用参数数组启动标准库服务，避免 shell 插值执行任意输入。
        with self.log_path.open("ab") as log:
            self.log_path.chmod(0o600)
            process = subprocess.Popen(
                [sys.executable, "-m", "http.server", str(self.port), "--bind", self.host, "--directory", str(self.report_root)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **popen_session_kwargs(),
            )
        state = {
            "pid": process.pid,
            "host": self.host,
            "port": self.port,
            "report_root": str(self.report_root),
            "process_group": True,
            "started_at": time.time(),
        }
        self._write_state(state)
        deadline = time.time() + 3
        identity_error = ""
        while time.time() < deadline:
            if process.poll() is not None:
                break
            if self._port_ready():
                inspection = inspect_process(process.pid)
                if inspection.status == "found":
                    stable_identity = _trusted_report_identity_for_command(
                        self.report_root,
                        self.host,
                        self.port,
                        inspection.command,
                    )
                    if stable_identity is not None:
                        state["process_executable"] = inspection.command[0]
                        self._write_state(state)
                        return {"ok": True, "already_running": False, **self.status()}
                    identity_error = "报告服务就绪后的进程命令与启动参数不匹配"
                elif inspection.status == "inspection_failed":
                    identity_error = inspection.error or "报告服务进程身份检查失败"
                else:
                    identity_error = "报告服务进程在身份确认前退出"
            time.sleep(0.05)
        return {
            "ok": False,
            **self.status(),
            "error": identity_error or "报告服务启动后未能监听端口",
            "log": str(self.log_path),
        }

    def status(self) -> dict[str, Any]:
        """Read persisted lifecycle state and verify both process and TCP port are alive."""
        state = self._read_state()
        pid = int(state.get("pid") or 0)
        host = str(state.get("host") or self.host)
        port = int(state.get("port") or self.port)
        owned = self._owns_process(pid, state)
        running = bool(owned and self._port_ready(port))
        return {
            "running": running,
            "pid": pid,
            "host": host,
            "port": port,
            "report_root": str(self.report_root),
            "local_url": f"http://127.0.0.1:{port}/",
            "lan_urls": [f"http://{address}:{port}/" for address in _lan_addresses()]
            if _is_unspecified_bind(host)
            else [],
            "log": str(self.log_path),
        }

    def stop(self) -> dict[str, Any]:
        """Stop only the process recorded for this report root and remove stale state."""
        state = self._read_state()
        pid = int(state.get("pid") or 0)
        if not self._static_identity_matches(state):
            return self._stop_result(
                pid,
                stopped=False,
                owned=False,
                ok=False,
                error="report server state does not match this manager",
            )
        inspection = inspect_process(pid)
        if inspection.status == "inspection_failed":
            return self._stop_result(
                pid,
                stopped=False,
                owned=False,
                ok=False,
                error=inspection.error,
            )
        if inspection.status == "not_found":
            self.state_path.unlink(missing_ok=True)
            return self._stop_result(pid, stopped=True, owned=False, ok=True)
        process_identity = _report_process_identity_for_inspection(
            self.report_root,
            self.host,
            self.port,
            state,
            inspection,
        )
        owned = process_identity is not None
        if owned:
            result = terminate_owned_process(
                pid,
                process_identity,
                process_group=state.get("process_group") is True,
            )
            exited = bool(result.get("ok", False))
        else:
            exited = False
        if exited:
            self.state_path.unlink(missing_ok=True)
        return self._stop_result(
            pid,
            stopped=exited,
            owned=owned,
            ok=bool(result.get("ok", False)) if owned else False,
            error=str(result.get("error") or "") if owned else "process ownership mismatch",
        )

    def _owns_process(self, pid: int, state: dict[str, Any]) -> bool:
        """Verify persisted metadata and the live command before trusting or terminating a PID."""
        if not self._static_identity_matches(state):
            return False
        inspection = inspect_process(pid)
        return (
            inspection.status == "found"
            and _report_process_identity_for_inspection(
                self.report_root,
                self.host,
                self.port,
                state,
                inspection,
            )
            is not None
        )

    def _static_identity_matches(self, state: dict[str, Any]) -> bool:
        """Validate persisted report root, port, and host without inspecting or signaling a PID."""
        try:
            state_root = Path(str(state.get("report_root") or "")).expanduser().resolve()
            state_port = int(state.get("port") or 0)
            state_host = str(state.get("host") or "")
        except (OSError, TypeError, ValueError):
            return False
        return state_root == self.report_root and state_port == self.port and state_host == self.host

    def _stop_result(
        self,
        pid: int,
        *,
        stopped: bool,
        owned: bool,
        ok: bool,
        error: str = "",
    ) -> dict[str, Any]:
        """Build the stable report-stop response while exposing safe inspection failures."""
        result: dict[str, Any] = {
            "ok": ok,
            "stopped": stopped,
            "pid": pid,
            "report_root": str(self.report_root),
            "ownership_verified": owned,
        }
        if error:
            result["error"] = error
        return result

    def _port_ready(self, port: int | None = None) -> bool:
        """Probe the configured listener locally without relying on external network access."""
        try:
            with socket.create_connection(("127.0.0.1", int(port or self.port)), timeout=0.15):
                return True
        except OSError:
            return False

    def _read_state(self) -> dict[str, Any]:
        """Read lifecycle metadata defensively so a partial file cannot break MCP startup."""
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_state(self, state: dict[str, Any]) -> None:
        """Persist lifecycle metadata atomically for later MCP processes."""
        atomic_write_private_text(self.state_path, json.dumps(state, ensure_ascii=False, indent=2))


def _matches_report_server_command(argv: list[str], report_root: Path, host: str, port: int) -> bool:
    """Match the identifying arguments of the stdlib HTTP server launched by this manager."""
    return _report_process_identity(report_root, host, port).matches(argv)


def _report_process_identity(
    report_root: Path,
    host: str,
    port: int,
    *,
    executable: str | None = None,
) -> ProcessIdentity:
    """Build the exact Python module and report-root argv identity for the managed server."""
    return ProcessIdentity(
        executable or Path(sys.executable).name,
        (
            "-m",
            "http.server",
            str(port),
            "--bind",
            host,
            "--directory",
            str(report_root.expanduser().resolve()),
        ),
    )


def _report_process_identity_from_state(
    report_root: Path,
    host: str,
    port: int,
    state: dict[str, Any],
) -> ProcessIdentity:
    """Use the stable native executable captured after readiness, with legacy fallback."""
    executable = str(state.get("process_executable") or "") or None
    return _report_process_identity(report_root, host, port, executable=executable)


def _report_process_identity_for_inspection(
    report_root: Path,
    host: str,
    port: int,
    state: dict[str, Any],
    inspection: ProcessInspection,
) -> ProcessIdentity | None:
    """Use persisted stable identity or safely derive legacy identity from exact live invariant args."""
    if inspection.status != "found":
        return None
    if state.get("process_executable"):
        persisted = _report_process_identity_from_state(report_root, host, port, state)
        return persisted if persisted.matches(inspection.command) else None
    return _trusted_report_identity_for_command(
        report_root,
        host,
        port,
        inspection.command,
    )


def _trusted_report_identity_for_command(
    report_root: Path,
    host: str,
    port: int,
    command: tuple[str, ...] | list[str],
) -> ProcessIdentity | None:
    """Match report argv only against configured Python and derived installed native launch paths."""
    for executable in trusted_python_executables(sys.executable):
        identity = _report_process_identity(
            report_root,
            host,
            port,
            executable=executable,
        )
        if identity.matches(command):
            return identity
    return None


def _lan_addresses() -> list[str]:
    """Return non-loopback IPv4 addresses suitable for same-LAN report access."""
    addresses: set[str] = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET):
            address = item[4][0]
            parsed = ipaddress.ip_address(address)
            if parsed.is_private and not parsed.is_loopback and not parsed.is_link_local:
                addresses.add(address)
    except socket.gaierror:
        pass
    return sorted(addresses)


def _is_unspecified_bind(host: str) -> bool:
    """Return whether a numeric bind address intentionally listens on every interface."""
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False
