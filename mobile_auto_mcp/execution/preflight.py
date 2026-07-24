"""Preflight checks for toolchain, device, and read-only proxy alignment."""

from __future__ import annotations

import shutil
import socket
import subprocess
from dataclasses import dataclass, asdict
from typing import Any

from mobile_auto_mcp.execution.failures import build_failure
from mobile_auto_mcp.platform.capabilities import host_capability
from mobile_auto_mcp.proxy.proxy_manager import DEFAULT_PORTS


_LAST_HDC_DISCOVERY: dict[str, Any] = {}
_SUPPORTED_TARGETS = {"android", "ios", "harmony"}


@dataclass
class PreflightResult:
    ok: bool
    target: str
    proxy_required: bool
    expected_proxy_port: int
    checks: dict[str, Any]
    blockers: list[str]
    warnings: list[str]
    failures: list[dict[str, Any]]
    phone_proxy_hint: str
    proxy_instruction: dict[str, Any]
    phone_proxy_mutation_allowed: bool = False
    phone_proxy_policy: str = "detect_and_prompt_only"

    def as_dict(self) -> dict[str, Any]:
        """Serialize this result into a JSON-compatible dictionary."""
        return asdict(self)


def run_preflight(
    target: str = "android",
    proxy_required: bool = True,
    proxy_port: int | None = None,
    device_serial: str = "",
    require_android_proxy_match: bool = True,
    wda_url: str = "",
    auto_start_wda: bool = False,
    allow_wda_reinstall: bool = False,
    wda_start_command: str = "",
    wda_iproxy_command: str = "",
) -> PreflightResult:
    """Run preflight without mutating the phone proxy settings."""
    normalized = target.lower()
    expected_port = int(proxy_port or DEFAULT_PORTS.get(normalized, 12999))
    capability = host_capability(normalized)
    if not capability["ok"]:
        # Unsupported iOS hosts stop before any WDA probe/start while other MCP lanes remain usable.
        blocker = (
            "platform_not_supported: iOS 自动化仅支持 macOS；"
            f"当前主机平台为 {capability['host_platform']}"
        )
        checks = {
            "host_capability": capability,
            "expected_proxy_port": expected_port,
            "device_driver_supported": normalized in _SUPPORTED_TARGETS,
        }
        proxy_instruction = build_proxy_instruction(normalized, expected_port, proxy_required)
        return PreflightResult(
            ok=False,
            target=normalized,
            proxy_required=proxy_required,
            expected_proxy_port=expected_port,
            checks=checks,
            blockers=[blocker],
            warnings=[],
            failures=[
                build_failure(
                    "platform_not_supported",
                    normalized,
                    "preflight",
                    evidence={"message": blocker, "checks": checks},
                )
            ],
            phone_proxy_hint=proxy_instruction["message"],
            proxy_instruction=proxy_instruction,
        )
    checks: dict[str, Any] = {
        "host_capability": capability,
        "mitmdump": bool(shutil.which("mitmdump")),
        "adb": bool(shutil.which("adb")),
        "hdc": bool(shutil.which("hdc")),
        "expected_proxy_port": expected_port,
        "proxy_port_free": _port_free(expected_port),
        "device_driver_supported": normalized in _SUPPORTED_TARGETS,
    }
    blockers: list[str] = []
    warnings: list[str] = []
    if proxy_required and not checks["mitmdump"]:
        blockers.append("未找到 mitmdump，无法启动 mitmproxy")
    if proxy_required and not checks["proxy_port_free"]:
        blockers.append(f"代理端口 {expected_port} 已被占用")
    if not checks["device_driver_supported"]:
        blockers.append(f"{_target_label(normalized)} 设备驱动尚未接入，不能执行自动化；请先接入对应设备后端")
    elif normalized == "android":
        if not checks["adb"]:
            blockers.append("未找到 adb，无法检查 Android 设备和 WLAN 代理")
        else:
            checks["android_devices"] = _adb_devices()
            if not checks["android_devices"]:
                blockers.append("未发现 Android 设备")
            if proxy_required and require_android_proxy_match:
                checks["android_proxy"] = read_android_proxy(device_serial=device_serial)
                ok, message = _android_proxy_matches(checks["android_proxy"], expected_port)
                checks["android_proxy_match"] = ok
                if not ok:
                    blockers.append(message)
            else:
                checks["android_proxy"] = {
                    "ok": None,
                    "deferred": True,
                    "message": "正式执行将在创建可恢复快照后，通过当前 Wi-Fi 系统设置页托管并复核代理",
                }
    elif normalized == "ios":
        # WDA modules stay behind the macOS capability gate so unsupported hosts can start the MCP.
        from mobile_auto_mcp.execution.adapters.ios import DEFAULT_WDA_URL
        from mobile_auto_mcp.execution.wda_guardian import (
            ensure_wda,
            resolve_iproxy_command,
            resolve_wda_start_command,
            wda_setup_hint,
        )

        checks["wda_url"] = wda_url or DEFAULT_WDA_URL
        checks["wda"] = _wda_status(checks["wda_url"])
        checks["wda"]["auto_start_requested"] = bool(auto_start_wda)
        checks["wda"]["reinstall_allowed"] = bool(allow_wda_reinstall)
        checks["wda"]["startable"] = bool(resolve_wda_start_command(wda_start_command, device_serial=device_serial))
        checks["wda"]["iproxy_configured"] = bool(resolve_iproxy_command(wda_iproxy_command, device_serial=device_serial))
        checks["wda"]["setup_hint"] = wda_setup_hint(checks["wda_url"])
        if auto_start_wda and not checks["wda"].get("ok"):
            checks["wda_start"] = ensure_wda(
                wda_url=checks["wda_url"],
                start_command=wda_start_command,
                iproxy_command=wda_iproxy_command,
                device_serial=device_serial,
                allow_wda_reinstall=allow_wda_reinstall,
            )
            if checks["wda_start"].get("ok"):
                checks["wda"] = {
                    **checks["wda_start"].get("wda", {}),
                    "auto_start_requested": True,
                    "startable": True,
                    "iproxy_configured": checks["wda_start"].get("iproxy_configured", False),
                    "setup_hint": wda_setup_hint(checks["wda_url"]),
                }
        if not checks["wda"].get("ok"):
            start_result = checks.get("wda_start") or {}
            diagnosis = start_result.get("diagnostics") or {}
            blockers.append(
                f"WDA 不可用："
                f"{diagnosis.get('error') or start_result.get('error') or checks['wda'].get('error') or checks['wda_url']}"
            )
        warnings.append("iOS 设备代理需人工确认；MCP 只检测 WDA，不会修改手机代理")
    elif normalized == "harmony":
        if not checks["hdc"]:
            blockers.append("未找到 hdc，无法检查 HarmonyOS 设备")
        else:
            checks["harmony_devices"] = _hdc_devices()
            checks["harmony_device_discovery"] = dict(_LAST_HDC_DISCOVERY) or {
                "ok": bool(checks["harmony_devices"]),
                "stage": "device_discovery",
                "command": ["hdc", "list", "targets"],
                "timed_out": False,
                "devices": checks["harmony_devices"],
            }
            if not checks["harmony_devices"]:
                detail = checks["harmony_device_discovery"]
                blockers.append(
                    "HarmonyOS 设备发现失败："
                    f"{detail.get('message') or detail.get('stderr') or detail.get('error_type') or '未发现在线设备'}"
                )
            elif device_serial and device_serial not in checks["harmony_devices"]:
                warnings.append(f"未在 hdc list targets 中发现指定 HarmonyOS 设备 {device_serial}，将尝试使用默认在线设备")
        warnings.append("HarmonyOS 设备代理需人工确认；MCP 只检测 hdc 连通性，不会修改手机代理")
    else:
        blockers.append(f"不支持的目标端: {target}")
    proxy_instruction = build_proxy_instruction(normalized, expected_port, proxy_required)
    hint = proxy_instruction["message"]
    failure_code = {
        "ios": "ios_runner_unavailable",
        "harmony": "harmony_device_unavailable",
    }.get(normalized, "preflight_blocked")
    failures = [
        build_failure(
            failure_code,
            normalized,
            "preflight",
            evidence={"message": blocker, "checks": checks},
        )
        for blocker in blockers
    ]
    return PreflightResult(
        ok=not blockers,
        target=normalized,
        proxy_required=proxy_required,
        expected_proxy_port=expected_port,
        checks=checks,
        blockers=blockers,
        warnings=warnings,
        failures=failures,
        phone_proxy_hint=hint,
        proxy_instruction=proxy_instruction,
    )


def read_android_proxy(device_serial: str = "") -> dict[str, Any]:
    """Read the connected Android Wi-Fi proxy through system Settings without global keys."""
    from mobile_auto_mcp.execution.devices import DeviceDriver

    return DeviceDriver(target="android", device_serial=device_serial).read_system_proxy()


def _android_proxy_matches(proxy: dict[str, Any], expected_port: int) -> tuple[bool, str]:
    """Require verified current-Wi-Fi manual proxy evidence, never a global setting."""
    if not proxy.get("ok"):
        return False, str(proxy.get("message") or "未能读取 Android 当前 Wi-Fi 代理，禁止继续")
    if str(proxy.get("mode") or "").lower() != "manual":
        return False, f"Android 当前 Wi-Fi 未启用手动代理，请设置为本机 IP:{expected_port} 后重跑"
    actual_port = int(proxy.get("port") or 0)
    if actual_port == int(expected_port):
        return True, ""
    return False, f"Android 当前 Wi-Fi 代理端口为 {actual_port or '未识别'}，与期望端口 {expected_port} 不一致"


def _adb_devices() -> list[str]:
    """Handle adb devices using the supplied state and inputs."""
    try:
        output = subprocess.check_output(["adb", "devices"], text=True, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    devices: list[str] = []
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def _hdc_devices() -> list[str]:
    """Handle hdc devices using the supplied state and inputs."""
    global _LAST_HDC_DISCOVERY
    command = ["hdc", "list", "targets"]
    try:
        completed = subprocess.run(command, text=True, capture_output=True, timeout=5, check=False)
    except subprocess.TimeoutExpired as exc:
        _LAST_HDC_DISCOVERY = {
            "ok": False,
            "stage": "device_discovery",
            "command": command,
            "exit_code": None,
            "stdout": str(exc.stdout or ""),
            "stderr": str(exc.stderr or ""),
            "timed_out": True,
            "error_type": exc.__class__.__name__,
            "message": "hdc 设备发现命令超时",
            "remediation": "确认鸿蒙设备已解锁、USB 调试已开启且授权未失效，然后执行 hdc list targets 验证连接",
        }
        return []
    except OSError as exc:
        _LAST_HDC_DISCOVERY = {
            "ok": False,
            "stage": "device_discovery",
            "command": command,
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "timed_out": False,
            "error_type": exc.__class__.__name__,
            "message": "无法执行 hdc 设备发现命令",
            "remediation": "确认 hdc 已安装并可从当前 PATH 执行",
        }
        return []
    devices = [line.strip().split()[0] for line in completed.stdout.splitlines() if line.strip() and not line.startswith("[")]
    ok = completed.returncode == 0 and bool(devices)
    _LAST_HDC_DISCOVERY = {
        "ok": ok,
        "stage": "device_discovery",
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout[-2000:],
        "stderr": completed.stderr[-2000:],
        "timed_out": False,
        "error_type": "" if completed.returncode == 0 else "HDCCommandError",
        "message": "" if ok else ("hdc 未返回在线设备" if completed.returncode == 0 else "hdc 设备发现命令失败"),
        "remediation": "确认设备已连接、已解锁并授权 USB 调试，然后重新执行 hdc list targets",
        "devices": devices,
    }
    return devices


def _adb_shell(args: list[str], device_serial: str = "") -> str:
    """Handle adb shell using the supplied state and inputs."""
    cmd = ["adb"]
    if device_serial:
        cmd += ["-s", device_serial]
    cmd += ["shell", *args]
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _port_free(port: int) -> bool:
    """Handle port free using the supplied state and inputs."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", int(port))) != 0


def build_proxy_instruction(target: str, expected_port: int, proxy_required: bool = True) -> dict[str, Any]:
    """Build proxy instruction using the supplied state and inputs."""
    candidates = _local_ip_candidates()
    host = candidates[0] if candidates else ""
    address = f"{host}:{expected_port}" if host else f"本机IP:{expected_port}"
    return {
        "target": target,
        "required": bool(proxy_required),
        "mitmproxy_port": int(expected_port),
        "phone_proxy_host": host or "本机IP",
        "phone_proxy_port": int(expected_port),
        "phone_proxy_address": address,
        "phone_proxy_host_candidates": candidates,
        "mutation_allowed": False,
        "policy": "standalone_preflight_read_only_runner_managed",
        "setup_before": ["启动 mitmproxy/mitmdump", "把手机 WLAN 代理指向 phone_proxy_address", "打开或重启 App 后再进入目标页"],
        "message": (
            f"本次 mitmproxy 端口：{expected_port}；"
            f"手机 WLAN 代理请设置为 {address}。"
            "独立 preflight 只读检测；正式 run_cases 将在保存快照后托管设置代理，执行后保留并提醒用户手动关闭。"
        ),
    }




def _local_ip_candidates() -> list[str]:
    """Handle local ip candidates using the supplied state and inputs."""
    candidates: list[str] = []
    for iface in ("en0", "en1", "en2"):
        try:
            value = subprocess.check_output(["ipconfig", "getifaddr", iface], text=True, stderr=subprocess.DEVNULL, timeout=1).strip()
        except (OSError, subprocess.SubprocessError):
            value = ""
        if value and not value.startswith("127.") and value not in candidates:
            candidates.append(value)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            value = str(sock.getsockname()[0])
            if value and not value.startswith("127.") and value not in candidates:
                candidates.append(value)
    except OSError:
        pass
    return candidates


def _wda_status(wda_url: str) -> dict[str, Any]:
    """Require strong WDA execution readiness after the macOS capability gate."""
    from mobile_auto_mcp.execution.adapters.ios import probe_wda_readiness

    return probe_wda_readiness(wda_url)






def _target_label(target: str) -> str:
    """Handle target label using the supplied state and inputs."""
    return {"ios": "iOS", "android": "Android", "harmony": "HarmonyOS"}.get(target, target)
