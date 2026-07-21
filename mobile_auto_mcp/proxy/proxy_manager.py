"""mitmproxy lifecycle management."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

from mobile_auto_mcp.platform.processes import (
    ProcessIdentity,
    popen_session_kwargs,
    process_command,
    terminate_owned_process,
)
from mobile_auto_mcp.state.private_files import atomic_write_private_text, ensure_private_directory


DEFAULT_PORTS = {"android": 13000, "ios": 13000, "harmony": 13000}


class ProxyManager:
    """Start and stop mitmdump with the bundled addon."""

    def __init__(self, home: str | Path, target: str, port: int | None = None) -> None:
        """Bind one managed proxy runtime to an isolated data home and listening port."""
        self.home = Path(home)
        self.target = target
        self.port = int(port or DEFAULT_PORTS.get(target, 12999))
        self.addon_path = Path(__file__).resolve().parent / "proxy_addon.py"
        self.runtime_path = self.home / "proxy" / "managed_proxy_runtime.json"
        self.process: subprocess.Popen | None = None
        self.reused_existing = False
        self._stdout = None
        self._stderr = None

    def start(self) -> dict[str, object]:
        """Start mitmdump or reuse a retained process only when durable ownership evidence matches."""
        if _is_port_open(self.port):
            runtime = self._owned_retained_runtime()
            if runtime:
                self.reused_existing = True
                return {"ok": True, "reused": True, "pid": int(runtime["pid"]), "port": self.port}
            raise RuntimeError(f"代理端口 {self.port} 已被占用，请先停止旧 mitmproxy/mitmdump 或换端口")
        env = os.environ.copy()
        env["MOBILE_AUTO_MCP_HOME"] = str(self.home)
        package_parent = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = package_parent + os.pathsep + env.get("PYTHONPATH", "")
        cmd = ["mitmdump", "-p", str(self.port), "-s", str(self.addon_path), "--set", "block_global=false"]
        log_dir = ensure_private_directory(self.home / "proxy")
        self._stdout = (log_dir / "mitmdump_stdout.log").open("a", encoding="utf-8")
        self._stderr = (log_dir / "mitmdump_stderr.log").open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            cmd,
            env=env,
            stdout=self._stdout,
            stderr=self._stderr,
            text=True,
            **popen_session_kwargs(),
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("mitmproxy 启动失败，请检查代理插件加载和本机 Python 环境")
            if _is_port_open(self.port):
                # Ownership metadata lets a later Runner safely reuse this intentionally retained process.
                atomic_write_private_text(
                    self.runtime_path,
                    json.dumps(
                        {
                            "pid": self.process.pid,
                            "port": self.port,
                            "home": str(self.home.resolve()),
                            "addon": str(self.addon_path.resolve()),
                            "started_at": time.time(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
                return {"ok": True, "reused": False, "pid": self.process.pid, "port": self.port}
            time.sleep(0.2)
        raise RuntimeError(f"mitmproxy 启动超时，端口 {self.port} 未进入监听状态")

    def stop(self) -> None:
        """Stop only a process started by this manager; retained reused processes remain externally managed."""
        if not self.process:
            return
        if self.process.poll() is None:
            result = terminate_owned_process(self.process.pid, _proxy_identity(self.addon_path))
            if not result.get("ok", False):
                return
        self.process = None
        for handle in (self._stdout, self._stderr):
            if handle:
                handle.close()
        self._stdout = None
        self._stderr = None
        self.runtime_path.unlink(missing_ok=True)

    def stop_owned_retained(self, runtime: dict[str, object] | None = None) -> dict[str, object]:
        """Stop a retained mitmdump only after durable home, port, addon, PID, and command ownership checks pass."""
        evidence = dict(runtime or self._read_runtime() or {})
        identity = self._validate_runtime_identity(evidence)
        if not identity.get("ok", False):
            return identity
        pid = int(evidence["pid"])
        if not _pid_exists(pid):
            # Static home/port/addon ownership is sufficient once the recorded process no longer exists;
            # no signal is sent, so PID-reuse protection is not weakened.
            self.runtime_path.unlink(missing_ok=True)
            return {"ok": True, "status": "already_stopped", "pid": pid, "port": self.port}
        result = terminate_owned_process(pid, _proxy_identity(self.addon_path))
        result.setdefault("port", self.port)
        if result.get("ok", False):
            self.runtime_path.unlink(missing_ok=True)
        return result

    def runtime_evidence(self) -> dict[str, object]:
        """Return persisted ownership metadata for durable retained-proxy recovery."""
        return dict(self._read_runtime() or {})

    @classmethod
    def stop_owned_runtime(cls, runtime: dict[str, object]) -> dict[str, object]:
        """Recreate a manager from persisted runtime evidence and stop its retained owned process."""
        try:
            home = str(runtime.get("home") or "")
            port = int(runtime.get("port") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "status": "invalid_runtime", "message": "代理运行记录缺少有效端口"}
        if not home or port <= 0:
            return {"ok": False, "status": "invalid_runtime", "message": "代理运行记录缺少 home 或端口"}
        return cls(home, target="android", port=port).stop_owned_retained(runtime)

    def _owned_retained_runtime(self) -> dict[str, object] | None:
        """Return retained runtime metadata only when home, port, addon, PID, and command still match."""
        try:
            runtime = json.loads(self.runtime_path.read_text(encoding="utf-8"))
            pid = int(runtime.get("pid") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        if int(runtime.get("port") or 0) != self.port:
            return None
        if str(runtime.get("home") or "") != str(self.home.resolve()):
            return None
        if str(runtime.get("addon") or "") != str(self.addon_path.resolve()):
            return None
        return runtime if pid > 0 and _pid_matches_runtime(pid, self.addon_path) else None

    def _read_runtime(self) -> dict[str, object] | None:
        """Read retained runtime evidence without treating malformed or non-object JSON as owned state."""
        try:
            runtime = json.loads(self.runtime_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return runtime if isinstance(runtime, dict) else None

    def _validate_runtime(self, runtime: dict[str, object]) -> dict[str, object]:
        """Validate persisted runtime identity before allowing a signal to reach its PID."""
        identity = self._validate_runtime_identity(runtime)
        if not identity.get("ok", False):
            return identity
        pid = int(runtime["pid"])
        # PID reuse is possible after the original Runner exits, so command identity is mandatory before signaling.
        if not _pid_matches_runtime(pid, self.addon_path):
            return {"ok": False, "status": "ownership_mismatch", "pid": pid}
        return {"ok": True, "status": "owned", "pid": pid, "port": self.port}

    def _validate_runtime_identity(self, runtime: dict[str, object]) -> dict[str, object]:
        """Validate static home, port, addon, and PID fields without assuming the process is still alive."""
        try:
            pid = int(runtime.get("pid") or 0)
            port = int(runtime.get("port") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "status": "invalid_runtime"}
        expected = {
            "home": str(self.home.resolve()),
            "addon": str(self.addon_path.resolve()),
            "port": self.port,
        }
        if pid <= 0 or port != expected["port"]:
            return {"ok": False, "status": "ownership_mismatch", "pid": pid}
        if str(runtime.get("home") or "") != expected["home"]:
            return {"ok": False, "status": "ownership_mismatch", "pid": pid}
        if str(runtime.get("addon") or "") != expected["addon"]:
            return {"ok": False, "status": "ownership_mismatch", "pid": pid}
        return {"ok": True, "status": "owned", "pid": pid, "port": port}


def _is_port_open(port: int) -> bool:
    """Return whether the local proxy port currently accepts TCP connections."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def _pid_matches_runtime(pid: int, addon_path: Path) -> bool:
    """Verify a retained PID still runs mitmdump with this project's exact addon path."""
    return _proxy_identity(addon_path).matches(process_command(pid))


def _pid_exists(pid: int) -> bool:
    """Return whether a PID still exists without sending a state-changing signal."""
    return bool(process_command(int(pid)))


def _proxy_identity(addon_path: Path) -> ProcessIdentity:
    """Build the exact addon argv identity required before stopping a managed mitmdump."""
    return ProcessIdentity("mitmdump", ("-s", str(addon_path.resolve())))
