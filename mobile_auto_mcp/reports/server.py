"""Persistent local HTTP service for generated report hubs."""

from __future__ import annotations

import ipaddress
import json
import os
import signal
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

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
                start_new_session=True,
            )
        self._write_state({"pid": process.pid, "host": self.host, "port": self.port, "report_root": str(self.report_root), "started_at": time.time()})
        deadline = time.time() + 3
        while time.time() < deadline:
            if process.poll() is not None:
                break
            if self._port_ready():
                return {"ok": True, "already_running": False, **self.status()}
            time.sleep(0.05)
        return {"ok": False, **self.status(), "error": "报告服务启动后未能监听端口", "log": str(self.log_path)}

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
            "lan_urls": [f"http://{address}:{port}/" for address in _lan_addresses()] if host in {"0.0.0.0", "::"} else [],
            "log": str(self.log_path),
        }

    def stop(self) -> dict[str, Any]:
        """Stop only the process recorded for this report root and remove stale state."""
        state = self._read_state()
        pid = int(state.get("pid") or 0)
        owned = self._owns_process(pid, state)
        if owned:
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            exited = _wait_for_exit(pid, timeout=3)
            if not exited and self._owns_process(pid, state):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                exited = _wait_for_exit(pid, timeout=1)
        else:
            exited = True
        self.state_path.unlink(missing_ok=True)
        return {
            "ok": True,
            "stopped": exited,
            "pid": pid,
            "report_root": str(self.report_root),
            "ownership_verified": owned,
        }

    def _owns_process(self, pid: int, state: dict[str, Any]) -> bool:
        """Verify persisted metadata and the live command before trusting or terminating a PID."""
        if not _pid_alive(pid):
            return False
        try:
            state_root = Path(str(state.get("report_root") or "")).expanduser().resolve()
            state_port = int(state.get("port") or 0)
            state_host = str(state.get("host") or "")
        except (OSError, TypeError, ValueError):
            return False
        if state_root != self.report_root or state_port != self.port or state_host != self.host:
            return False
        try:
            command = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                check=True,
                capture_output=True,
                text=True,
                timeout=1,
            ).stdout.strip()
            argv = shlex.split(command)
        except (OSError, subprocess.SubprocessError, ValueError):
            return False
        return _matches_report_server_command(argv, self.report_root, self.host, self.port)

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


def _pid_alive(pid: int) -> bool:
    """Check process existence without sending a terminating signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _matches_report_server_command(argv: list[str], report_root: Path, host: str, port: int) -> bool:
    """Match the identifying arguments of the stdlib HTTP server launched by this manager."""
    try:
        module_index = argv.index("-m")
        directory_index = argv.index("--directory")
        bind_index = argv.index("--bind")
        actual_root = Path(argv[directory_index + 1]).expanduser().resolve()
        return (
            argv[module_index + 1] == "http.server"
            and argv[module_index + 2] == str(port)
            and argv[bind_index + 1] == host
            and actual_root == report_root
        )
    except (IndexError, OSError, ValueError):
        return False


def _wait_for_exit(pid: int, *, timeout: float) -> bool:
    """Wait for process exit and reap it when this manager is the direct parent."""
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        try:
            waited_pid, _ = os.waitpid(pid, os.WNOHANG)
            if waited_pid == pid:
                return True
        except ChildProcessError:
            pass
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)
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
