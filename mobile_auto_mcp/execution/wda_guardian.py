"""Keep iOS WebDriverAgent reachable across MCP tool calls."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
from typing import Any

from mobile_auto_mcp.execution.devices import DEFAULT_WDA_URL, IOSWDAClient, WDAConnectionError


DEFAULT_WDA_PROCESS_PATTERN = "xcodebuild .*WebDriverAgentRunner|WebDriverAgentRunner-Runner"
DEFAULT_IPROXY_PROCESS_PATTERN = "iproxy .*8100"
DEFAULT_WDA_LOG_DIR = Path(
    os.environ.get("MOBILE_AUTO_MCP_WDA_LOG_DIR") or Path(tempfile.gettempdir()) / "mobile_auto_mcp_wda"
)


def ensure_wda(
    *,
    wda_url: str = "",
    start_command: str = "",
    iproxy_command: str = "",
    device_serial: str = "",
    restart_on_failure: bool = True,
    allow_wda_reinstall: bool = False,
    timeout: float = 20,
    interval: float = 1,
) -> dict[str, Any]:
    """Ensure WDA is reachable without reinstalling its runner by default."""
    resolved_url = (wda_url or os.environ.get("MOBILE_AUTO_MCP_WDA_URL") or os.environ.get("WDA_URL") or DEFAULT_WDA_URL).rstrip("/")
    resolved_start = resolve_wda_start_command(start_command, device_serial=device_serial)
    resolved_iproxy = resolve_iproxy_command(iproxy_command, device_serial=device_serial)

    status = _wda_status(resolved_url)
    if status.get("ok"):
        return {
            "ok": True,
            "action": "reuse",
            "wda_url": resolved_url,
            "wda": status,
            "startable": bool(resolved_start),
            "iproxy_configured": bool(resolved_iproxy),
        }

    cleanup: dict[str, Any] = {}
    starts: list[dict[str, Any]] = []
    if resolved_iproxy:
        if restart_on_failure:
            cleanup = _stop_existing_processes([DEFAULT_IPROXY_PROCESS_PATTERN])
        starts.append(
            {
                "role": "iproxy",
                **_start_process(
                    _render_command(resolved_iproxy, device_serial, resolved_url),
                    device_serial=device_serial,
                ),
            }
        )
        if not starts[-1].get("ok"):
            return _start_failure_result(
                resolved_url=resolved_url,
                status=status,
                cleanup=cleanup,
                starts=starts,
                resolved_start=resolved_start,
                resolved_iproxy=resolved_iproxy,
                allow_wda_reinstall=allow_wda_reinstall,
            )

        recovered = wait_wda_ready(resolved_url, timeout=min(timeout, 5), interval=interval)
        if recovered.get("ok"):
            return {
                "ok": True,
                "action": "iproxy_recovered",
                "wda_url": resolved_url,
                "wda": recovered,
                "cleanup": cleanup,
                "starts": starts,
                "startable": bool(resolved_start),
                "iproxy_configured": True,
                "reinstall_allowed": bool(allow_wda_reinstall),
            }
        status = recovered

    if not allow_wda_reinstall:
        return {
            "ok": False,
            "action": "wda_runner_unavailable",
            "wda_url": resolved_url,
            "wda": status,
            "cleanup": cleanup,
            "starts": starts,
            "error": (
                "WDA Runner 当前不可用；常规执行不会自动重装、重新签名或重启 WDA。"
                "请保持已安装的 Runner 常驻，或显式执行 WDA 修复。"
            ),
            "setup_hint": wda_setup_hint(resolved_url),
            "startable": bool(resolved_start),
            "iproxy_configured": bool(resolved_iproxy),
            "reinstall_allowed": False,
        }

    if not resolved_start:
        return {
            "ok": False,
            "action": "blocked",
            "wda_url": resolved_url,
            "wda": status,
            "cleanup": cleanup,
            "starts": starts,
            "error": "已允许 WDA 修复，但未配置或发现 WDA 启动命令",
            "setup_hint": wda_setup_hint(resolved_url),
            "startable": False,
            "iproxy_configured": bool(resolved_iproxy),
            "reinstall_allowed": True,
        }

    starts.append(
        {
            "role": "wda",
            **_start_process(
                _render_command(resolved_start, device_serial, resolved_url),
                device_serial=device_serial,
            ),
        }
    )

    failed = [item for item in starts if not item.get("ok")]
    if failed:
        return _start_failure_result(
            resolved_url=resolved_url,
            status=status,
            cleanup=cleanup,
            starts=starts,
            resolved_start=resolved_start,
            resolved_iproxy=resolved_iproxy,
            allow_wda_reinstall=allow_wda_reinstall,
        )

    ready = wait_wda_ready(resolved_url, timeout=timeout, interval=interval)
    diagnostics = _startup_diagnostics(starts, ready)
    return {
        "ok": bool(ready.get("ok")),
        "action": "started" if ready.get("ok") else diagnostics.get("action", "start_timeout"),
        "wda_url": resolved_url,
        "wda": ready,
        "cleanup": cleanup,
        "starts": starts,
        "diagnostics": diagnostics,
        "error": "" if ready.get("ok") else diagnostics.get("error", "WDA 启动后未就绪"),
        "startable": True,
        "iproxy_configured": bool(resolved_iproxy),
        "reinstall_allowed": True,
        "setup_hint": wda_setup_hint(resolved_url),
    }


def _start_failure_result(
    *,
    resolved_url: str,
    status: dict[str, Any],
    cleanup: dict[str, Any],
    starts: list[dict[str, Any]],
    resolved_start: str,
    resolved_iproxy: str,
    allow_wda_reinstall: bool,
) -> dict[str, Any]:
    """Start failure result using the supplied state and inputs."""
    diagnostics = _startup_diagnostics(starts, status)
    failed = [item for item in starts if not item.get("ok")]
    return {
        "ok": False,
        "action": "start_failed",
        "wda_url": resolved_url,
        "wda": status,
        "cleanup": cleanup,
        "starts": starts,
        "diagnostics": diagnostics,
        "error": diagnostics.get("error") or failed[0].get("error") or "WDA 连接恢复命令执行失败",
        "setup_hint": wda_setup_hint(resolved_url),
        "startable": bool(resolved_start),
        "iproxy_configured": bool(resolved_iproxy),
        "reinstall_allowed": bool(allow_wda_reinstall),
    }


def wait_wda_ready(wda_url: str, timeout: float = 20, interval: float = 1) -> dict[str, Any]:
    """Handle wait wda ready using the supplied state and inputs."""
    deadline = time.time() + timeout
    last = {"ok": False, "error": "WDA 启动后未就绪"}
    while time.time() < deadline:
        last = _wda_status(wda_url)
        if last.get("ok"):
            return last
        time.sleep(interval)
    return last


def wda_setup_hint(wda_url: str) -> str:
    """Handle wda setup hint using the supplied state and inputs."""
    return (
        f"请先启动 WebDriverAgent 并确保 {wda_url}/status 可访问；"
        "MCP 常规执行只复用已就绪的 Runner 并恢复 iproxy，不会自动重装或重签 WDA；"
        "只有显式允许 WDA 修复时，才会使用 MOBILE_AUTO_MCP_WDA_START_CMD "
        "或自动发现的启动命令。"
    )


def _wda_status(wda_url: str) -> dict[str, Any]:
    """Handle wda status using the supplied state and inputs."""
    try:
        return {"ok": True, "status": IOSWDAClient(wda_url).status()}
    except WDAConnectionError as exc:
        return {"ok": False, "error": str(exc)}


def resolve_wda_start_command(command: str = "", *, device_serial: str = "") -> str:
    """Resolve wda start command using the supplied state and inputs."""
    explicit = (command or os.environ.get("MOBILE_AUTO_MCP_WDA_START_CMD") or os.environ.get("WDA_START_CMD") or "").strip()
    if explicit:
        return explicit
    project = _discover_wda_project()
    if not project:
        return ""
    destination = "id={device_serial}" if device_serial else "generic/platform=iOS"
    return (
        f"xcodebuild -project {shlex.quote(str(project))} "
        "-scheme WebDriverAgentRunner "
        f"-destination {shlex.quote(destination)} "
        "test-without-building"
    )


def resolve_iproxy_command(command: str = "", *, device_serial: str = "") -> str:
    """Resolve iproxy command using the supplied state and inputs."""
    explicit = (command or os.environ.get("MOBILE_AUTO_MCP_IPROXY_CMD") or os.environ.get("IPROXY_START_CMD") or "").strip()
    if explicit:
        return explicit
    if device_serial and _which("iproxy"):
        return "iproxy 8100:8100 -u {device_serial}"
    return ""


def build_keepalive_wda_command(
    *,
    project: Path,
    device_serial: str,
    xcodebuild: str = "xcodebuild",
) -> list[str]:
    """Build the non-repair command used to relaunch an existing signed WDA product."""
    return [
        xcodebuild,
        "-project",
        str(project),
        "-scheme",
        "WebDriverAgentRunner",
        "-destination",
        f"id={device_serial}",
        "test-without-building",
    ]


def run_wda_keepalive(
    *,
    wda_url: str,
    project: Path,
    device_serial: str,
    xcodebuild: str = "xcodebuild",
    interval: float = 5,
    log_file: Path | None = None,
) -> None:
    """Keep WDA reachable without rebuilding, reinstalling, resigning, or killing it."""
    command = build_keepalive_wda_command(
        project=project,
        device_serial=device_serial,
        xcodebuild=xcodebuild,
    )
    output = log_file or DEFAULT_WDA_LOG_DIR / "keepalive-xcodebuild.log"
    output.parent.mkdir(parents=True, exist_ok=True)

    while True:
        if _wda_status(wda_url.rstrip("/")).get("ok"):
            time.sleep(interval)
            continue

        with output.open("ab") as handle:
            subprocess.run(
                command,
                stdout=handle,
                stderr=subprocess.STDOUT,
                check=False,
            )
        time.sleep(interval)


def _main() -> int:
    """Run the module command-line entry point."""
    parser = argparse.ArgumentParser(description="Keep an existing signed WDA runner alive")
    subparsers = parser.add_subparsers(dest="command", required=True)
    keepalive = subparsers.add_parser("keepalive")
    keepalive.add_argument("--wda-url", default=DEFAULT_WDA_URL)
    keepalive.add_argument("--project", type=Path, required=True)
    keepalive.add_argument("--device-serial", required=True)
    keepalive.add_argument("--xcodebuild", default="xcodebuild")
    keepalive.add_argument("--interval", type=float, default=5)
    keepalive.add_argument("--log-file", type=Path)
    args = parser.parse_args()

    if args.command == "keepalive":
        run_wda_keepalive(
            wda_url=args.wda_url,
            project=args.project,
            device_serial=args.device_serial,
            xcodebuild=args.xcodebuild,
            interval=args.interval,
            log_file=args.log_file,
        )
    return 0






def _render_command(command: str, device_serial: str, wda_url: str) -> str:
    """Render command using the supplied state and inputs."""
    return command.format(device_serial=device_serial, udid=device_serial, wda_url=wda_url)


def _start_process(command: str, *, device_serial: str = "") -> dict[str, Any]:
    """Start process using the supplied state and inputs."""
    if not command:
        return {"ok": False, "error": "启动命令为空"}
    env = os.environ.copy()
    if device_serial:
        env["UDID"] = device_serial
    log_file = _next_log_file("wda-process")
    try:
        log_handle = log_file.open("ab")
        process = subprocess.Popen(
            shlex.split(command),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_handle.close()
    except (OSError, ValueError) as exc:
        return {"ok": False, "command": command, "error": str(exc), "log_file": str(log_file)}
    return {"ok": True, "command": command, "pid": process.pid, "log_file": str(log_file)}


def _stop_existing_processes(patterns: list[str]) -> dict[str, Any]:
    """Stop existing processes using the supplied state and inputs."""
    results: list[dict[str, Any]] = []
    for pattern in patterns:
        completed = subprocess.run(["pkill", "-f", pattern], text=True, capture_output=True)
        results.append({"pattern": pattern, "exit_code": completed.returncode})
    return {"ok": True, "results": results}


if __name__ == "__main__":
    raise SystemExit(_main())


def _startup_diagnostics(starts: list[dict[str, Any]], status: dict[str, Any]) -> dict[str, Any]:
    """Handle startup diagnostics using the supplied state and inputs."""
    log_text = "\n".join(_read_tail(Path(str(item.get("log_file")))) for item in starts if item.get("log_file"))
    classified = _classify_wda_log(log_text)
    diagnostics: dict[str, Any] = {
        "ok": not classified,
        "status": status,
        "log_files": [item.get("log_file") for item in starts if item.get("log_file")],
    }
    if classified:
        diagnostics.update(classified)
    elif not status.get("ok"):
        diagnostics.update(
            {
                "action": "start_timeout",
                "category": "wda_start_timeout",
                "error": f"WDA 启动后未就绪：{status.get('error') or 'status 不可访问'}",
                "remediation": ["检查真机是否解锁、USB 是否稳定、WDA Runner 是否正在前台运行。"],
            }
        )
    return diagnostics


def _classify_wda_log(text: str) -> dict[str, Any]:
    """Handle classify wda log using the supplied state and inputs."""
    normalized = text.lower()
    if "not been explicitly trusted by the user" in normalized or "developer app certificate" in normalized:
        return {
            "action": "device_trust_required",
            "category": "ios_developer_certificate_untrusted",
            "error": "WDA Runner 被真机安全策略拒绝启动：开发者证书/描述文件尚未在设备上显式信任。",
            "remediation": [
                "在 iPhone 打开 设置 -> 通用 -> VPN 与设备管理，信任对应 Apple Development 证书。",
                "信任后保持设备解锁，再重新执行 auto_start_wda 或 prepare_run。",
            ],
        }
    if "timed out while enabling automation mode" in normalized:
        return {
            "action": "automation_mode_timeout",
            "category": "ios_automation_mode_timeout",
            "error": "Xcode 启用 iOS UI Automation 模式超时，WDA Runner 未完成初始化。",
            "remediation": ["保持设备解锁并停留前台，重新插拔 USB 或重启 WDA Runner 后重试。"],
        }
    if "developerimage" in normalized or "developer disk image" in normalized:
        return {
            "action": "developer_image_required",
            "category": "ios_developer_image_missing",
            "error": "本机缺少或未挂载当前 iOS 版本可用的 Developer Disk Image。",
            "remediation": ["确认 Xcode 支持该 iOS 版本，并先用 Xcode 打开设备完成开发环境准备。"],
        }
    if "address already in use" in normalized:
        return {
            "action": "port_busy",
            "category": "wda_port_busy",
            "error": "WDA 或 iproxy 端口被占用。",
            "remediation": ["关闭占用 8100 的旧进程后重试。"],
        }
    if "error connecting to device" in normalized:
        return {
            "action": "iproxy_device_unreachable",
            "category": "iproxy_device_connection_failed",
            "error": "iproxy 无法连接指定 iOS 真机。",
            "remediation": ["确认 UDID 正确、设备在线并已信任当前电脑。"],
        }
    return {}


def _discover_wda_project() -> Path | None:
    """Handle discover wda project using the supplied state and inputs."""
    env_path = os.environ.get("MOBILE_AUTO_MCP_WDA_PROJECT") or os.environ.get("WDA_PROJECT")
    candidates = [Path(env_path)] if env_path else []
    candidates.extend(
        [
            Path.cwd() / "WebDriverAgent.xcodeproj",
            Path.cwd() / "WebDriverAgent" / "WebDriverAgent.xcodeproj",
            Path.home() / "code" / "WebDriverAgent" / "WebDriverAgent.xcodeproj",
            Path.home() / "WebDriverAgent" / "WebDriverAgent.xcodeproj",
        ]
    )
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    return None


def _next_log_file(prefix: str) -> Path:
    """Handle next log file using the supplied state and inputs."""
    DEFAULT_WDA_LOG_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_WDA_LOG_DIR / f"{prefix}-{int(time.time() * 1000)}.log"


def _read_tail(path: Path, max_bytes: int = 60000) -> str:
    """Read tail using the supplied state and inputs."""
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes), os.SEEK_SET)
        return handle.read().decode("utf-8", errors="ignore")


def _which(name: str) -> str:
    """Return the resolved executable path from the current environment."""
    return subprocess.run(["which", name], text=True, capture_output=True).stdout.strip()
