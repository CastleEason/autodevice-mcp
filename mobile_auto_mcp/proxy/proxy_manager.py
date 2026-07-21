"""mitmproxy lifecycle management."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

from mobile_auto_mcp.platform.processes import (
    ProcessIdentity,
    ProcessInspection,
    inspect_process,
    popen_session_kwargs,
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
        runtime: dict[str, object] = {
            "pid": self.process.pid,
            "port": self.port,
            "home": str(self.home.resolve()),
            "addon": str(self.addon_path.resolve()),
            "process_group": True,
            "expected_executable": "mitmdump",
            "expected_arguments": ["-s", str(self.addon_path.resolve())],
            "identity_state": "provisional",
            "started_at": time.time(),
        }
        try:
            atomic_write_private_text(
                self.runtime_path,
                json.dumps(runtime, ensure_ascii=False, indent=2),
            )
        except OSError:
            self._stop_untracked_spawn()
            raise
        deadline = time.time() + 5
        identity_error = ""
        while time.time() < deadline:
            if self.process.poll() is not None:
                self._fail_start("mitmproxy 启动失败，请检查代理插件加载和本机 Python 环境")
            if _is_port_open(self.port):
                inspection = inspect_process(self.process.pid)
                stable_fields = _stable_proxy_identity_fields(self.addon_path, inspection)
                if stable_fields is None:
                    identity_error = (
                        inspection.error
                        if inspection.status == "inspection_failed"
                        else "mitmproxy 就绪后的进程命令与启动参数不匹配"
                    )
                    time.sleep(0.05)
                    continue
                # Ownership metadata lets a later Runner safely reuse this intentionally retained process.
                runtime.update(stable_fields)
                runtime["identity_state"] = "stable"
                try:
                    atomic_write_private_text(
                        self.runtime_path,
                        json.dumps(runtime, ensure_ascii=False, indent=2),
                    )
                except OSError:
                    self._fail_start("mitmproxy 稳定进程身份持久化失败")
                return {"ok": True, "reused": False, "pid": self.process.pid, "port": self.port}
            time.sleep(0.2)
        if identity_error:
            self._fail_start(f"mitmproxy 进程身份确认失败：{identity_error}")
        self._fail_start(f"mitmproxy 启动超时，端口 {self.port} 未进入监听状态")

    def stop(self) -> None:
        """Stop only a process started by this manager; retained reused processes remain externally managed."""
        if not self.process:
            return
        if self.process.poll() is None:
            result = self.stop_owned_retained(self._read_runtime())
            if not result.get("ok", False):
                return
        self.process = None
        for handle in (self._stdout, self._stderr):
            if handle:
                handle.close()
        self._stdout = None
        self._stderr = None
        self.runtime_path.unlink(missing_ok=True)

    def _fail_start(self, message: str) -> NoReturn:
        """Attempt evidence-gated cleanup after startup failure and retain state when proof is unavailable."""
        cleanup = self.stop_owned_retained(self._read_runtime())
        if cleanup.get("ok", False):
            self.process = None
        for handle in (self._stdout, self._stderr):
            if handle:
                handle.close()
        self._stdout = None
        self._stderr = None
        raise RuntimeError(message)

    def _stop_untracked_spawn(self) -> None:
        """Stop the directly owned Popen child if provisional evidence cannot be persisted."""
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        self.process = None
        for handle in (self._stdout, self._stderr):
            if handle:
                handle.close()
        self._stdout = None
        self._stderr = None

    def stop_owned_retained(self, runtime: dict[str, object] | None = None) -> dict[str, object]:
        """Stop a retained mitmdump only after durable home, port, addon, PID, and command ownership checks pass."""
        evidence = dict(runtime or self._read_runtime() or {})
        identity = self._validate_runtime_identity(evidence)
        if not identity.get("ok", False):
            return identity
        pid = int(evidence["pid"])
        inspection = inspect_process(pid)
        if inspection.status == "not_found":
            # Static home/port/addon ownership is sufficient once the recorded process no longer exists;
            # no signal is sent, so PID-reuse protection is not weakened.
            self.runtime_path.unlink(missing_ok=True)
            return {"ok": True, "status": "already_stopped", "pid": pid, "port": self.port}
        if inspection.status == "inspection_failed":
            return {
                "ok": False,
                "status": "inspection_failed",
                "pid": pid,
                "port": self.port,
                "error": inspection.error,
            }
        process_identity = _proxy_identity_for_inspection(self.addon_path, evidence, inspection)
        if process_identity is None:
            return {"ok": False, "status": "ownership_mismatch", "pid": pid, "port": self.port}
        result = terminate_owned_process(
            pid,
            process_identity,
            process_group=evidence.get("process_group") is True,
        )
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
        return runtime if pid > 0 and _pid_matches_runtime(pid, self.addon_path, runtime) else None

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
        inspection = inspect_process(pid)
        if inspection.status == "inspection_failed":
            return {
                "ok": False,
                "status": "inspection_failed",
                "pid": pid,
                "port": self.port,
                "error": inspection.error,
            }
        if inspection.status == "not_found":
            return {"ok": True, "status": "already_stopped", "pid": pid, "port": self.port}
        # PID reuse is possible after the original Runner exits, so command identity is mandatory before signaling.
        if _proxy_identity_for_inspection(self.addon_path, runtime, inspection) is None:
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
        process_executable = str(runtime.get("process_executable") or "")
        process_program = str(runtime.get("process_program") or "")
        if bool(process_executable) != bool(process_program):
            return {"ok": False, "status": "ownership_mismatch", "pid": pid}
        expected_executable = runtime.get("expected_executable")
        expected_arguments = runtime.get("expected_arguments")
        if expected_executable is not None and expected_executable != "mitmdump":
            return {"ok": False, "status": "ownership_mismatch", "pid": pid}
        if expected_arguments is not None and expected_arguments != ["-s", expected["addon"]]:
            return {"ok": False, "status": "ownership_mismatch", "pid": pid}
        return {"ok": True, "status": "owned", "pid": pid, "port": port}


def _is_port_open(port: int) -> bool:
    """Return whether the local proxy port currently accepts TCP connections."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def _pid_matches_runtime(
    pid: int,
    addon_path: Path,
    runtime: dict[str, object] | None = None,
) -> bool:
    """Verify a retained PID still runs mitmdump with this project's exact addon path."""
    inspection = inspect_process(pid)
    return (
        inspection.status == "found"
        and _proxy_identity_for_inspection(addon_path, runtime or {}, inspection) is not None
    )


def _pid_exists(pid: int) -> bool:
    """Return whether a PID still exists without sending a state-changing signal."""
    return inspect_process(int(pid)).status == "found"


def _proxy_identity(
    addon_path: Path,
    runtime: dict[str, object] | None = None,
) -> ProcessIdentity:
    """Build the exact addon argv identity required before stopping a managed mitmdump."""
    evidence = runtime or {}
    process_executable = str(evidence.get("process_executable") or "")
    process_program = str(evidence.get("process_program") or "")
    if process_executable and process_program:
        launchers = () if process_executable == process_program else (process_executable,)
        return ProcessIdentity(
            process_program,
            ("-s", str(addon_path.resolve())),
            launcher_executables=launchers,
        )
    return ProcessIdentity(
        "mitmdump",
        ("-s", str(addon_path.resolve())),
        launcher_executables=(Path(sys.executable).name,),
    )


def _stable_proxy_identity_fields(
    addon_path: Path,
    inspection: ProcessInspection,
) -> dict[str, str] | None:
    """Capture the native launcher/program pair only after the ready PID matches owned proxy args."""
    if inspection.status != "found":
        return None
    command = inspection.command
    required_arguments = ("-s", str(addon_path.resolve()))
    direct = ProcessIdentity("mitmdump", required_arguments)
    if direct.matches(command):
        return {
            "process_executable": command[0],
            "process_program": command[0],
        }
    if len(command) > 1:
        launched = ProcessIdentity(
            "mitmdump",
            required_arguments,
            launcher_executables=(command[0],),
        )
        if launched.matches(command):
            return {
                "process_executable": command[0],
                "process_program": command[1],
            }
    return None


def _proxy_identity_for_inspection(
    addon_path: Path,
    runtime: dict[str, object],
    inspection: ProcessInspection,
) -> ProcessIdentity | None:
    """Use persisted stable identity or safely derive legacy identity from exact live invariant args."""
    if inspection.status != "found":
        return None
    persisted = _proxy_identity(addon_path, runtime)
    if persisted.matches(inspection.command):
        return persisted
    if runtime.get("process_executable") or runtime.get("process_program"):
        return None
    stable_fields = _stable_proxy_identity_fields(addon_path, inspection)
    if stable_fields is None:
        return None
    derived = _proxy_identity(addon_path, stable_fields)
    return derived if derived.matches(inspection.command) else None
