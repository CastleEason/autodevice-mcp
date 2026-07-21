"""Device automation facade used by MCP tools and runner."""

from __future__ import annotations

import subprocess
import time
import re
import json
import os
import tempfile
import ipaddress
from pathlib import Path
from typing import Any

from mobile_auto_mcp.execution.adapters.harmony import HDCCommandError, HarmonyHDCClient
from mobile_auto_mcp.execution.adapters.ios import (
    DEFAULT_WDA_URL,  # noqa: F401 - compatibility re-export used by wda_guardian and existing callers
    IOSWDAClient,
    WDAConnectionError,
    WDARequestTimeoutError,
    WDATapTimeoutError,
)
from mobile_auto_mcp.execution.command_runner import run_configured_command
from mobile_auto_mcp.execution.failures import build_failure


SUPPORTED_TARGETS = {"android", "ios", "harmony"}


class UnsupportedTargetError(RuntimeError):
    """Raised when a target has no concrete automation backend."""




class DeviceDriver:
    """Small cross-target facade; Android uses uiautomator2 when available."""

    def __init__(
        self,
        target: str = "android",
        device_serial: str = "",
        wda_url: str = "",
        ios_tap_backend: str = "",
        ios_tap_command: str = "",
        auto_start_wda: bool = False,
        wda_start_command: str = "",
        wda_iproxy_command: str = "",
        visual_locator: Any | None = None,
        evidence_dir: str | Path = "",
        post_action_verifier: Any | None = None,
        visual_min_confidence: float | None = None,
        visual_ambiguity_margin: float | None = None,
        locator_tree_limit: int | None = None,
    ) -> None:
        """Initialize DeviceDriver state, configuration, and runtime dependencies."""
        self.target = target.lower()
        self.device_serial = device_serial
        self.wda_url = wda_url
        self.ios_tap_backend = ios_tap_backend
        self.ios_tap_command = ios_tap_command
        self.auto_start_wda = auto_start_wda or _env_truthy("MOBILE_AUTO_MCP_WDA_AUTO_ENSURE")
        self.wda_start_command = wda_start_command
        self.wda_iproxy_command = wda_iproxy_command
        vision_command = os.environ.get("MOBILE_AUTO_MCP_VISION_LOCATOR_COMMAND") or ""
        self.visual_locator = visual_locator or (_command_visual_locator(vision_command) if vision_command else None)
        self.evidence_dir = Path(evidence_dir) if evidence_dir else Path(tempfile.gettempdir()) / "mobile_auto_mcp_evidence"
        self.post_action_verifier = post_action_verifier
        self.visual_min_confidence = float(visual_min_confidence if visual_min_confidence is not None else os.environ.get("MOBILE_AUTO_MCP_VISUAL_MIN_CONFIDENCE") or 0.8)
        self.visual_ambiguity_margin = float(visual_ambiguity_margin if visual_ambiguity_margin is not None else os.environ.get("MOBILE_AUTO_MCP_VISUAL_AMBIGUITY_MARGIN") or 0.05)
        self.locator_tree_limit = max(1, int(locator_tree_limit if locator_tree_limit is not None else os.environ.get("MOBILE_AUTO_MCP_LOCATOR_TREE_LIMIT") or 500))
        self.wda_guardian_result: dict[str, Any] | None = None
        self._device: Any | None = None

    def connect(self) -> Any | None:
        """Connect to the selected platform automation backend."""
        self._ensure_supported()
        if self.target == "ios":
            if self._device is None:
                if self.auto_start_wda:
                    from mobile_auto_mcp.execution.wda_guardian import ensure_wda

                    self.wda_guardian_result = ensure_wda(
                        wda_url=self.wda_url,
                        start_command=self.wda_start_command,
                        iproxy_command=self.wda_iproxy_command,
                        device_serial=self.device_serial,
                    )
                    if not self.wda_guardian_result.get("ok"):
                        raise WDAConnectionError(str(self.wda_guardian_result.get("error") or self.wda_guardian_result))
                self._device = IOSWDAClient(
                    self.wda_url,
                    device_udid=self.device_serial,
                    tap_backend=self.ios_tap_backend,
                    tap_command=self.ios_tap_command,
                )
            return self._device
        if self.target == "harmony":
            if self._device is None:
                self._device = HarmonyHDCClient(self.device_serial)
            return self._device
        if self._device is not None:
            return self._device
        try:
            import uiautomator2 as u2

            self._device = u2.connect(self.device_serial or None)
        except Exception:
            self._device = None
        return self._device

    def launch_app(self, target_app_package: str, wait_seconds: float = 3) -> dict[str, Any]:
        """Launch the requested application package or bundle."""
        self._ensure_supported()
        if not target_app_package:
            raise ValueError("target_app_package 是启动 App 的显式能力字段，不能为空")
        device = self.connect()
        if self.target == "ios":
            result = device.launch_app(target_app_package)
            time.sleep(wait_seconds)
            return {**result, "current_app": self.current_app()}
        if self.target == "harmony":
            try:
                result = device.launch_app(target_app_package)
            except HDCCommandError as exc:
                if exc.code != "harmony_command_timeout":
                    raise
                time.sleep(wait_seconds)
                failure = build_failure(
                    exc.code,
                    self.target,
                    "launch_app",
                    evidence={"command": exc.command, "detail": exc.detail},
                )
                return {
                    "ok": True,
                    "verified": False,
                    "response_timeout": True,
                    "strategy": "hdc_launch_timeout_continue",
                    "bundle": target_app_package,
                    "warning": "HarmonyOS 启动命令响应超时，动作可能已经生效；继续执行语义导航并由后续页面锚点确认。",
                    "failure": failure,
                }
            time.sleep(wait_seconds)
            try:
                current_app = self.current_app()
            except HDCCommandError as exc:
                failure = build_failure(
                    exc.code,
                    self.target,
                    "foreground_probe",
                    evidence={"command": exc.command, "detail": exc.detail},
                )
                return {
                    **result,
                    "current_app": {},
                    "foreground_check": {"ok": False, "degraded": True, "failure": failure},
                }
            return {**result, "current_app": current_app, "foreground_check": {"ok": True, "degraded": False}}
        if device is not None:
            device.app_start(target_app_package)
        else:
            self._adb(["shell", "monkey", "-p", target_app_package, "-c", "android.intent.category.LAUNCHER", "1"])
        time.sleep(wait_seconds)
        return {"ok": True, "package": target_app_package, "current_app": self.current_app()}

    def current_app(self) -> dict[str, Any]:
        """Return the application currently in the foreground."""
        self._ensure_supported()
        device = self.connect()
        if self.target == "ios":
            return device.current_app()
        if self.target == "harmony":
            return device.current_app()
        if device is not None:
            try:
                return device.app_current()
            except Exception:
                pass
        output = self._adb(["shell", "dumpsys", "window"], check=False)
        return {"raw": output[-1200:]}

    def list_elements(self, limit: int = 80) -> list[dict[str, Any]]:
        """Return a bounded snapshot of visible UI elements."""
        self._ensure_supported()
        device = self.connect()
        if self.target == "ios":
            return device.list_elements(limit=limit)
        if self.target == "harmony":
            return device.list_elements(limit=limit)
        if device is None:
            return []
        rows: list[dict[str, Any]] = []
        try:
            for node in device.xpath("//*").all()[:limit]:
                info = node.attrib
                rows.append(
                    {
                        "text": info.get("text") or "",
                        "resource_id": info.get("resource-id") or "",
                        "content_desc": info.get("content-desc") or "",
                        "class": info.get("class") or "",
                        "clickable": info.get("clickable") == "true",
                        "selected": info.get("selected") == "true",
                        "enabled": info.get("enabled") != "false",
                        "bounds": info.get("bounds") or "",
                    }
                )
        except Exception:
            return rows
        return rows

    def click(self, text: str = "", resource_id: str = "", x: int | None = None, y: int | None = None) -> dict[str, Any]:
        """Click the requested UI target."""
        self._ensure_supported()
        if self.target == "ios":
            if x is None or y is None:
                selected, candidates = _select_element(self.list_elements(limit=self.locator_tree_limit), {"text": text, "resource_id": resource_id})
                if not selected:
                    return {"ok": False, "reason": "element_not_found", "candidates": candidates[:5]}
                x, y = _bounds_center(str(selected.get("bounds") or ""))
            if x is None or y is None:
                return {"ok": False, "reason": "element_has_no_bounds", "selected": selected}
            device = self.connect()
            try:
                return device.tap(int(x), int(y))
            except WDATapTimeoutError as exc:
                return self._ios_tap_timeout_result(device, int(x), int(y), exc)
        if self.target == "harmony":
            if x is None or y is None:
                selected, candidates = _select_element(self.list_elements(limit=self.locator_tree_limit), {"text": text, "resource_id": resource_id})
                if not selected:
                    return {"ok": False, "reason": "element_not_found", "candidates": candidates[:5]}
                x, y = _bounds_center(str(selected.get("bounds") or ""))
            if x is None or y is None:
                return {"ok": False, "reason": "element_has_no_bounds"}
            device = self.connect()
            try:
                return device.tap(int(x), int(y))
            except HDCCommandError as exc:
                return self._harmony_tap_timeout_result(device, int(x), int(y), exc)
        device = self.connect()
        if device is None:
            if x is not None and y is not None:
                self._adb(["shell", "input", "tap", str(x), str(y)])
                return {"ok": True, "strategy": "adb_xy"}
            return {"ok": False, "reason": "device_not_connected"}
        if resource_id and device(resourceId=resource_id).exists:
            device(resourceId=resource_id).click()
            return {"ok": True, "strategy": "resource_id", "resource_id": resource_id}
        if text and device(text=text).exists:
            device(text=text).click()
            return {"ok": True, "strategy": "text", "text": text}
        if text:
            candidates = device.xpath(f"//*[contains(@text, '{text}')]").all()
            if candidates:
                candidates[0].click()
                return {"ok": True, "strategy": "xpath_text_contains", "text": text}
        if x is not None and y is not None:
            device.click(x, y)
            return {"ok": True, "strategy": "xy"}
        return {"ok": False, "reason": "element_not_found"}

    def input_text(self, text: str, locator: dict[str, Any] | None = None, clear: bool = True) -> dict[str, Any]:
        """Focus a semantic field and replace its value without business-specific coordinates."""
        self._ensure_supported()
        device = self.connect()
        if locator:
            focused = self.resolve_and_click(locator)
            if not focused.get("ok"):
                return {"ok": False, "reason": "input_field_not_found", "focus": focused}
        if self.target in {"ios", "harmony"}:
            return device.input_text(str(text), clear=clear)
        if device is not None:
            try:
                device.send_keys(str(text), clear=clear)
                return {"ok": True, "strategy": "uiautomator2_send_keys"}
            except TypeError:
                device.send_keys(str(text))
                return {"ok": True, "strategy": "uiautomator2_send_keys_compat"}
        escaped = str(text).replace(" ", "%s")
        self._adb(["shell", "input", "text", escaped], check=False)
        return {"ok": True, "strategy": "adb_input_text"}

    def read_system_proxy(self) -> dict[str, Any]:
        """Open current Wi-Fi proxy settings semantically and read mode plus visible field values."""
        if self.target not in {"ios", "harmony"}:
            return {"ok": False, "message": "Android 系统代理由 ADB 适配器读取"}
        opened = self._open_system_proxy_settings()
        if not opened.get("ok"):
            return opened
        elements = self.list_elements(limit=self.locator_tree_limit)
        mode = _selected_proxy_mode(elements)
        if not mode:
            return {"ok": False, "message": "无法从系统设置识别 HTTP 代理模式", "elements": elements[:20]}
        fields = _proxy_text_fields(elements)
        host = str(fields[0].get("text") or "") if mode == "manual" and fields else ""
        port_text = str(fields[1].get("text") or "") if mode == "manual" and len(fields) > 1 else ""
        auto_url = str(fields[0].get("text") or "") if mode == "auto" and fields else ""
        try:
            port = int(port_text or 0)
        except ValueError:
            port = 0
        return {
            "ok": True,
            "target": self.target,
            "ssid": str(opened.get("ssid") or ""),
            "mode": mode,
            "host": host,
            "port": port,
            "auto_config_url": auto_url,
            "evidence": {"settings_navigation": opened, "field_count": len(fields)},
        }

    def read_wifi_network_info(self) -> dict[str, Any]:
        """Read the selected device's Wi-Fi IPv4 address from its semantic settings details page."""
        if self.target not in {"ios", "harmony"}:
            return {"ok": False, "message": "Android Wi-Fi 地址由设备级 ADB 命令读取"}
        opened = self._open_wifi_details()
        if not opened.get("ok"):
            return opened
        elements = self.list_elements(limit=self.locator_tree_limit)
        device_ip = _wifi_device_interface(elements)
        if not device_ip:
            return {
                "ok": False,
                "target": self.target,
                "ssid": str(opened.get("ssid") or ""),
                "message": "Wi-Fi 详情页未找到可验证的设备 IPv4 地址",
                "elements": elements[:20],
            }
        return {
            "ok": True,
            "target": self.target,
            "ssid": str(opened.get("ssid") or ""),
            "device_ip": device_ip,
            "source": "semantic_settings",
            "evidence": {"settings_navigation": opened},
        }

    def configure_system_proxy(
        self,
        *,
        mode: str,
        host: str = "",
        port: int = 0,
        auto_config_url: str = "",
    ) -> dict[str, Any]:
        """Configure off, manual, or automatic Wi-Fi proxy using localized semantic labels and fields."""
        normalized = str(mode or "none").lower()
        if normalized not in {"none", "manual", "auto"}:
            return {"ok": False, "message": f"不支持的系统代理模式: {mode}"}
        opened = self._open_system_proxy_settings()
        if not opened.get("ok"):
            return opened
        labels = {
            "none": ["关闭", "无", "Off", "None"],
            "manual": ["手动", "Manual"],
            "auto": ["自动", "Auto", "Automatic"],
        }
        selected = self.resolve_and_click({"any_text": labels[normalized], "target_description": "HTTP proxy mode"})
        if not selected.get("ok"):
            return {"ok": False, "message": "无法选择 HTTP 代理模式", "selection": selected}
        if normalized == "manual":
            fields = _proxy_text_fields(self.list_elements(limit=self.locator_tree_limit))
            if len(fields) < 2:
                return {"ok": False, "message": "手动代理页缺少服务器或端口输入框", "fields": fields}
            host_result = self.input_text(host, locator={"bounds": fields[0].get("bounds")}, clear=True)
            port_result = self.input_text(str(int(port)), locator={"bounds": fields[1].get("bounds")}, clear=True)
            if not host_result.get("ok") or not port_result.get("ok"):
                return {"ok": False, "message": "手动代理字段写入失败", "host": host_result, "port": port_result}
        elif normalized == "auto":
            fields = _proxy_text_fields(self.list_elements(limit=self.locator_tree_limit))
            if not fields:
                return {"ok": False, "message": "自动代理页缺少 URL 输入框"}
            url_result = self.input_text(auto_config_url, locator={"bounds": fields[0].get("bounds")}, clear=True)
            if not url_result.get("ok"):
                return {"ok": False, "message": "自动代理 URL 写入失败", "url": url_result}
        if self.target == "harmony":
            save = self.resolve_and_click({"any_text": ["保存", "Save", "确定", "OK"], "target_description": "save proxy settings"})
            if not save.get("ok"):
                return {"ok": False, "message": "HarmonyOS 代理设置未找到保存操作", "save": save}
        else:
            self.back()
        return {"ok": True, "target": self.target, "mode": normalized, "host": host, "port": int(port or 0)}

    def _open_system_proxy_settings(self) -> dict[str, Any]:
        """Navigate to the connected Wi-Fi HTTP proxy page using only system labels and live element state."""
        details = self._open_wifi_details()
        if not details.get("ok"):
            return details
        proxy = self.resolve_and_click(
            {"any_text": ["配置代理", "Configure Proxy", "HTTP 代理", "HTTP Proxy", "代理"], "target_description": "HTTP proxy settings"}
        )
        if not proxy.get("ok"):
            return {"ok": False, "message": "无法进入 HTTP 代理配置页", "proxy": proxy, "details": details}
        return {**details, "proxy": proxy}

    def _open_wifi_details(self) -> dict[str, Any]:
        """Navigate only to the connected Wi-Fi details page so address proof and proxy edits share one safe seam."""
        bundle = "com.apple.Preferences" if self.target == "ios" else "com.huawei.hmos.settings/MainAbility"
        launched = self.launch_app(bundle, wait_seconds=1)
        wifi = self.resolve_and_click({"any_text": ["无线局域网", "Wi-Fi", "WLAN"], "target_description": "system Wi-Fi settings"})
        if not wifi.get("ok"):
            return {"ok": False, "message": "无法进入系统 Wi-Fi 设置", "launch": launched, "wifi": wifi}
        elements = self.list_elements(limit=self.locator_tree_limit)
        detail, _ = _select_element(
            elements,
            {"any_text": ["更多信息", "More Info", "详情", "Details", "已连接", "Connected"]},
        )
        connected = next(
            (
                item
                for item in elements
                if item.get("selected")
                and str(item.get("text") or "") not in {"无线局域网", "Wi-Fi", "WLAN", "更多信息", "More Info", "详情", "Details"}
            ),
            None,
        )
        ssid = str((connected or {}).get("text") or "").strip()
        if not detail:
            detail = connected
        if not detail:
            return {"ok": False, "message": "无法识别当前已连接 Wi-Fi 的详情入口", "elements": elements[:20]}
        detail_click = self.resolve_and_click({"bounds": detail.get("bounds"), "target_description": "connected Wi-Fi details"})
        if not detail_click.get("ok"):
            return {"ok": False, "message": "无法打开当前 Wi-Fi 详情", "detail": detail_click}
        return {"ok": True, "ssid": ssid, "launch": launched, "wifi": wifi, "detail": detail_click}

    def swipe(
        self,
        x1: int | None = None,
        y1: int | None = None,
        x2: int | None = None,
        y2: int | None = None,
        duration: float = 0.2,
        *,
        x1_percent: float | None = None,
        y1_percent: float | None = None,
        x2_percent: float | None = None,
        y2_percent: float | None = None,
    ) -> dict[str, Any]:
        """Swipe with absolute coordinates or percentages resolved from the live window."""
        self._ensure_supported()
        device = self.connect()
        coordinates = (x1, y1, x2, y2)
        percentages = (x1_percent, y1_percent, x2_percent, y2_percent)
        if any(value is None for value in coordinates) and all(value is not None for value in percentages):
            width = height = 0
            if device is not None:
                width, height = device.window_size()
            else:
                output = str(self._adb(["shell", "wm", "size"], check=False))
                match = re.search(r"(\d+)x(\d+)", output)
                if match:
                    width, height = int(match.group(1)), int(match.group(2))
            if width <= 0 or height <= 0:
                return {"ok": False, "reason": "window_size_unavailable"}
            x1, y1 = int(width * float(x1_percent)), int(height * float(y1_percent))
            x2, y2 = int(width * float(x2_percent)), int(height * float(y2_percent))
        if any(value is None for value in (x1, y1, x2, y2)):
            return {"ok": False, "reason": "swipe_coordinates_required"}
        if device is None:
            self._adb(
                ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(int(float(duration) * 1000))],
                check=False,
            )
            strategy = "adb_swipe"
        elif self.target in {"ios", "harmony"}:
            result = device.swipe(int(x1), int(y1), int(x2), int(y2), float(duration))
            if isinstance(result, dict) and not result.get("ok", False):
                return result
            strategy = f"{self.target}_swipe"
        else:
            device.swipe(int(x1), int(y1), int(x2), int(y2), float(duration))
            strategy = "android_swipe"
        return {
            "ok": True,
            "strategy": strategy,
            "x1": int(x1),
            "y1": int(y1),
            "x2": int(x2),
            "y2": int(y2),
            "duration": float(duration),
        }

    def screenshot(self, path: str | Path) -> str:
        """Capture a device screenshot at the requested path."""
        self._ensure_supported()
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        device = self.connect()
        if self.target == "ios":
            return device.screenshot(out)
        if self.target == "harmony":
            return device.screenshot(out)
        if device is not None:
            device.screenshot(str(out))
        else:
            png = self._adb(["exec-out", "screencap", "-p"], text=False, check=False)
            if isinstance(png, bytes) and png:
                out.write_bytes(png)
            else:
                out.write_bytes(b"")
        return str(out)

    def navigate_path(self, path: list[dict[str, Any]]) -> dict[str, Any]:
        """Execute each step in a reusable navigation path."""
        self._ensure_supported()
        steps: list[dict[str, Any]] = []
        stopped_at_step: int | None = None
        stop_reason = ""
        for step_index, step in enumerate(path or []):
            before = step.get("before") or []
            if before:
                assertion = self.assert_page(before)
                if not assertion.get("ok"):
                    steps.append(
                        {
                            "ok": False,
                            "verified": False,
                            "action": "assert_before",
                            "code": "navigation_drift",
                            "assertion": assertion,
                            "failure": build_failure("navigation_drift", self.target, "navigate", evidence={"assertion": assertion}),
                        }
                    )
                    stopped_at_step = step_index
                    stop_reason = "navigation_drift"
                    break
            action = step.get("action") or "click"
            try:
                if action == "launch_app":
                    steps.append(
                        self.launch_app(
                            str(step.get("target_app_package") or step.get("package") or ""),
                            wait_seconds=float(step.get("wait_seconds", 3)),
                        )
                    )
                elif action == "sleep":
                    seconds = float(step.get("seconds") or 1)
                    time.sleep(seconds)
                    steps.append({"ok": True, "action": "sleep", "seconds": seconds})
                elif action == "back":
                    result = self.back()
                    steps.append(result)
                    wait_seconds = float(step.get("wait_seconds") or step.get("wait_after") or 0.3)
                    if result.get("ok") and wait_seconds > 0:
                        time.sleep(wait_seconds)
                elif action == "swipe":
                    result = self.swipe(
                        x1=step.get("x1"),
                        y1=step.get("y1"),
                        x2=step.get("x2"),
                        y2=step.get("y2"),
                        duration=float(step.get("duration") or 0.2),
                        x1_percent=step.get("x1_percent"),
                        y1_percent=step.get("y1_percent"),
                        x2_percent=step.get("x2_percent"),
                        y2_percent=step.get("y2_percent"),
                    )
                    steps.append(result)
                    wait_seconds = float(step.get("wait_seconds") or step.get("wait_after") or 0.3)
                    if result.get("ok") and wait_seconds > 0:
                        time.sleep(wait_seconds)
                elif action == "click":
                    if step.get("resolve", True):
                        result = self.resolve_and_click(step)
                    else:
                        result = self.click(
                            text=str(step.get("text") or ""),
                            resource_id=str(step.get("resource_id") or step.get("resourceId") or ""),
                            x=step.get("x"),
                            y=step.get("y"),
                        )
                    steps.append(result)
                    wait_seconds = float(step.get("wait_seconds") or step.get("wait_after") or 0.3)
                    if result.get("ok") and wait_seconds > 0:
                        time.sleep(wait_seconds)
                elif action == "tap_refresh":
                    steps.append(self.tap_refresh())
                elif action == "assert_page":
                    assertions = step.get("assertions") or step.get("assert_after") or []
                    assertion = self.assert_page(assertions)
                    steps.append({"ok": assertion.get("ok", False), "action": "assert_page", "assertion": assertion, "verified": assertion.get("ok", False)})
                elif action == "ensure_selected":
                    result = self.ensure_selected(step)
                    steps.append(result)
                    wait_seconds = float(step.get("wait_seconds") or step.get("wait_after") or 0.3)
                    if result.get("ok") and wait_seconds > 0:
                        time.sleep(wait_seconds)
            except HDCCommandError as exc:
                failure = build_failure(
                    exc.code,
                    self.target,
                    action,
                    evidence={"command": exc.command, "detail": exc.detail},
                )
                steps.append(
                    {
                        "ok": False,
                        "verified": False,
                        "action": action,
                        "reason": exc.code,
                        "failure": failure,
                    }
                )
                stopped_at_step = step_index
                stop_reason = exc.code
                break
            after = step.get("after") or step.get("assert_after") or []
            may_have_executed = bool(steps and steps[-1].get("response_timeout"))
            if after and steps and (steps[-1].get("ok") or may_have_executed):
                assertion = self.assert_page(after)
                verified = bool(assertion.get("ok"))
                if may_have_executed and verified:
                    steps[-1].update({"ok": True, "verified": True, "strategy": "tap_timeout_verified"})
                steps.append(
                    {
                        "ok": verified,
                        "action": "assert_after",
                        "assertion": assertion,
                        "verified": verified,
                        **({} if verified else {
                            "code": "action_not_effective",
                            "failure": build_failure("action_not_effective", self.target, "verify_action", evidence={"assertion": assertion}),
                        }),
                    }
                )
                if not verified:
                    stopped_at_step = step_index
                    stop_reason = "action_not_effective"
                    break
            elif after and steps and not steps[-1].get("ok", False):
                stopped_at_step = step_index
                stop_reason = str(steps[-1].get("reason") or steps[-1].get("code") or "action_failed")
                break
            elif steps and not steps[-1].get("ok", False):
                stopped_at_step = step_index
                stop_reason = str(steps[-1].get("reason") or steps[-1].get("code") or "action_failed")
                break
        verified_steps = [item for item in steps if item.get("verified") is True or item.get("action", "").startswith("assert")]
        result = {
            "ok": all(item.get("ok", False) for item in steps),
            "verified": bool(verified_steps) and all(item.get("ok", False) for item in verified_steps),
            "steps": steps,
        }
        if stopped_at_step is not None:
            result.update({"stopped_at_step": stopped_at_step, "stop_reason": stop_reason})
        return result

    def ensure_selected(self, step: dict[str, Any]) -> dict[str, Any]:
        """Ensure a generic selectable element is selected, clicking only when needed."""
        limit = int(step.get("limit") or 160)
        resolve_timeout = float(step.get("resolve_timeout_seconds") or 8)
        deadline = time.time() + resolve_timeout
        selected: dict[str, Any] | None = None
        candidates: list[dict[str, Any]] = []
        while time.time() < deadline:
            elements = self.list_elements(limit=limit)
            selected, candidates = _select_element(elements, step)
            if selected:
                break
            time.sleep(0.3)
        if not selected:
            return {
                "ok": False,
                "action": "ensure_selected",
                "reason": "element_not_resolved",
                "locator": _locator_from_step(step),
                "candidates": candidates[:5],
                "verified": False,
            }
        if not selected.get("selected"):
            x, y = _bounds_center(str(selected.get("bounds") or ""))
            click = self.click(x=x, y=y) if x is not None and y is not None else self.click(
                text=str(selected.get("text") or ""),
                resource_id=str(selected.get("resource_id") or ""),
            )
            if not click.get("ok"):
                return {
                    "ok": False,
                    "action": "ensure_selected",
                    "reason": "selection_click_failed",
                    "locator": _locator_from_step(step),
                    "selected": selected,
                    "click": click,
                    "verified": False,
                }
            elements = self.list_elements(limit=limit)
            selected_after, candidates_after = _select_element(elements, {**step, "selected": True})
            return {
                "ok": bool(selected_after and selected_after.get("selected")),
                "action": "ensure_selected",
                "locator": _locator_from_step(step),
                "before": selected,
                "click": click,
                "selected": selected_after,
                "candidates": candidates_after[:5],
                "verified": bool(selected_after and selected_after.get("selected")),
            }
        return {
            "ok": True,
            "action": "ensure_selected",
            "locator": _locator_from_step(step),
            "selected": selected,
            "candidates": candidates[:5],
            "strategy": "already_selected",
            "verified": True,
        }

    def back(self) -> dict[str, Any]:
        """Perform the platform's generic back navigation without business locators."""
        self._ensure_supported()
        device = self.connect()
        if self.target == "harmony":
            return device.back()
        if self.target == "ios":
            width, height = device.window_size()
            if width <= 0 or height <= 0:
                return {"ok": False, "strategy": "ios_back_gesture", "reason": "window_size_unavailable"}
            device.swipe(max(1, width // 50), height // 2, width * 3 // 4, height // 2, 0.2)
            return {"ok": True, "strategy": "ios_back_gesture"}
        if device is not None:
            device.press("back")
            return {"ok": True, "strategy": "android_back"}
        self._adb(["shell", "input", "keyevent", "4"], check=False)
        return {"ok": True, "strategy": "adb_back"}

    def resolve_and_click(self, step: dict[str, Any]) -> dict[str, Any]:
        """Resolve and click using the supplied state and inputs."""
        located = self.locate(step, limit=int(step.get("limit") or 120))
        if not located.get("ok"):
            return {**located, "action": "resolve_click", "verified": False}
        selected = located["selected"]
        candidates = located.get("candidates") or []
        x, y = _bounds_center(str(selected.get("bounds") or ""))
        if x is not None and y is not None:
            click = self.click(x=x, y=y)
        else:
            click = self.click(text=str(selected.get("text") or ""), resource_id=str(selected.get("resource_id") or ""))
        return {
            "ok": bool(click.get("ok")),
            "action": "resolve_click",
            "locator": _locator_from_step(step),
            "selected": selected,
            "candidates": candidates[:5],
            "confidence": selected.get("_confidence", 0),
            "locate_strategy": located.get("strategy"),
            **({"recovery": located["recovery"]} if located.get("recovery") else {}),
            "click": click,
            "verified": bool(click.get("ok")),
        }

    def locate(self, locator: dict[str, Any], limit: int = 120) -> dict[str, Any]:
        """Locate generically through element tree, bounded recovery, then visual evidence."""
        recovery: dict[str, Any] | None = None
        failure_code = ""
        evidence: dict[str, Any] = {"locator": _locator_from_step(locator)}
        try:
            selected, candidates = _select_element(self.list_elements(limit=limit), locator)
            if selected:
                return {"ok": True, "strategy": "element_tree", "selected": selected, "candidates": candidates[:5]}
        except WDARequestTimeoutError as exc:
            failure_code = "ios_source_timeout"
            evidence["error"] = str(exc)
            evidence["endpoint"] = "/source"
        except HDCCommandError as exc:
            failure_code = exc.code
            evidence.update({"error": exc.detail, "command": exc.command})

        if failure_code:
            skip_layout_retry = self.target == "harmony" and failure_code == "harmony_layout_timeout"
            recovery = (
                {
                    "ok": False,
                    "action": "skip_concurrent_layout_retry",
                    "reason": "HarmonyOS UiTest may still be running after the local timeout; do not start a concurrent dumpLayout.",
                }
                if skip_layout_retry
                else self.recover_driver(failure_code)
            )
            if recovery.get("ok") and not skip_layout_retry:
                try:
                    selected, candidates = _select_element(self.list_elements(limit=limit), locator)
                    if selected:
                        return {
                            "ok": True,
                            "strategy": "element_tree_after_recovery",
                            "selected": selected,
                            "candidates": candidates[:5],
                            "recovery": recovery,
                        }
                except (WDARequestTimeoutError, HDCCommandError) as exc:
                    evidence["retry_error"] = str(exc)

        screenshot_path = Path(self.evidence_dir) / f"locate_{self.target}_{int(time.time() * 1000)}.png"
        image_path = ""
        try:
            image_path = self.screenshot(screenshot_path)
            evidence["screenshot"] = image_path
        except Exception as exc:
            evidence["screenshot_error"] = str(exc)

        if image_path and callable(getattr(self, "visual_locator", None)):
            try:
                rows = self.visual_locator(image_path=image_path, locator=_locator_from_step(locator), target=self.target) or []
                if isinstance(rows, dict):
                    rows = rows.get("candidates") or ([rows.get("selected")] if rows.get("selected") else [])
                selected, candidates, visual_failure = _select_visual_candidate(
                    [row for row in rows if isinstance(row, dict)],
                    locator,
                    image_path,
                    min_confidence=float(getattr(self, "visual_min_confidence", 0.8)),
                    ambiguity_margin=float(getattr(self, "visual_ambiguity_margin", 0.05)),
                )
                if visual_failure:
                    evidence.update({"screenshot": image_path, "visual_candidates": candidates[:5]})
                    return {
                        "ok": False,
                        "reason": "visual_candidate_rejected",
                        "locator": _locator_from_step(locator),
                        "candidates": candidates[:5],
                        "failure": build_failure(visual_failure, self.target, "locate", evidence=evidence, attempts=[recovery] if recovery else []),
                        **({"recovery": recovery} if recovery else {}),
                    }
                if selected:
                    return {
                        "ok": True,
                        "strategy": "visual_locator",
                        "selected": selected,
                        "candidates": candidates[:5],
                        "evidence": {"screenshot": image_path},
                        **({"recovery": recovery} if recovery else {}),
                    }
            except Exception as exc:
                evidence["visual_error"] = str(exc)

        code = failure_code or "visual_locator_unavailable"
        attempts = [recovery] if recovery else []
        return {
            "ok": False,
            "reason": "element_not_resolved",
            "locator": _locator_from_step(locator),
            "candidates": [],
            "failure": build_failure(code, self.target, "locate", evidence=evidence, attempts=attempts),
            **({"recovery": recovery} if recovery else {}),
        }

    def recover_driver(self, code: str) -> dict[str, Any]:
        """Recover only this driver and keep every attempt bounded."""
        if self.target == "ios" and code == "ios_source_timeout":
            device = self.connect()
            if hasattr(device, "reset_session"):
                try:
                    return device.reset_session()
                except WDAConnectionError as exc:
                    return {"ok": False, "action": "recreate_session", "error": str(exc)}
        if self.target == "harmony" and code.startswith("harmony_"):
            self._device = None
            try:
                output = subprocess.check_output(["hdc", "list", "targets"], text=True, stderr=subprocess.STDOUT, timeout=4)
            except (OSError, subprocess.SubprocessError) as exc:
                return {"ok": False, "action": "rediscover_device", "error": str(exc)}
            targets = [line.strip() for line in output.splitlines() if line.strip() and "Empty" not in line]
            if self.device_serial and self.device_serial not in targets:
                return {"ok": False, "action": "rediscover_device", "targets": targets, "error": "configured target is offline"}
            if not self.device_serial and len(targets) == 1:
                self.device_serial = targets[0]
            self._device = HarmonyHDCClient(self.device_serial)
            return {"ok": bool(targets), "action": "rediscover_device", "targets": targets}
        return {"ok": False, "action": "none", "code": code}

    def assert_page(self, assertions: list[Any]) -> dict[str, Any]:
        """Handle assert page using the supplied state and inputs."""
        elements = self.list_elements(limit=160)
        passed: list[dict[str, Any]] = []
        missing: list[Any] = []
        for assertion in assertions or []:
            selected, candidates = _select_element(elements, assertion if isinstance(assertion, dict) else {"text": str(assertion)})
            if selected and _selected_state_matches(selected, assertion):
                passed.append({"assertion": assertion, "selected": selected, "candidates": candidates[:5]})
            else:
                missing.append({"assertion": assertion, "candidates": candidates[:5]})
        return {"ok": not missing, "verified": not missing, "passed": passed, "missing": missing}

    def tap_refresh(self) -> dict[str, Any]:
        """Perform an explicit refresh gesture using only the live screen size."""
        self._ensure_supported()
        device = self.connect()
        if self.target in {"ios", "harmony"}:
            width, height = device.window_size()
            if not width or not height:
                return {"ok": False, "reason": "window_size_unavailable", "strategy": f"{self.target}_pull_to_refresh"}
            device.swipe(width // 2, height // 4, width // 2, height * 2 // 3, 0.2)
            return {"ok": True, "strategy": f"{self.target}_pull_to_refresh"}
        if device is not None:
            width, height = device.window_size()
            if not width or not height:
                return {"ok": False, "reason": "window_size_unavailable", "strategy": "pull_to_refresh"}
            device.swipe(width // 2, height // 4, width // 2, height * 2 // 3, 0.2)
            return {"ok": True, "strategy": "pull_to_refresh"}
        output = str(self._adb(["shell", "wm", "size"], check=False))
        match = re.search(r"(\d+)x(\d+)", output)
        if not match:
            return {"ok": False, "reason": "window_size_unavailable", "strategy": "adb_pull_to_refresh"}
        width, height = int(match.group(1)), int(match.group(2))
        self._adb(["shell", "input", "swipe", str(width // 2), str(height // 4), str(width // 2), str(height * 2 // 3), "200"], check=False)
        return {"ok": True, "strategy": "adb_pull_to_refresh"}

    def close_popup(self, locators: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Close a popup only from caller-supplied semantic locators."""
        self._ensure_supported()
        for locator in locators or []:
            result = self.resolve_and_click(locator)
            if result.get("ok"):
                return result
        return {"ok": False, "reason": "popup_locator_required" if not locators else "popup_not_resolved"}

    def _ensure_supported(self) -> None:
        """Ensure supported using the supplied state and inputs."""
        target = getattr(self, "target", "android")
        if target not in SUPPORTED_TARGETS:
            raise UnsupportedTargetError(f"{target} 设备驱动尚未接入，不能回退到 Android/ADB 执行")

    def _ios_tap_timeout_result(self, device: Any, x: int, y: int, exc: WDATapTimeoutError) -> dict[str, Any]:
        """Handle ios tap timeout result using the supplied state and inputs."""
        timestamp = int(time.time() * 1000)
        evidence: dict[str, Any] = {}
        screenshot_path = Path(tempfile.gettempdir()) / f"mobile_auto_mcp_ios_tap_timeout_{timestamp}.png"
        try:
            evidence["screenshot"] = device.screenshot(screenshot_path)
        except Exception as screenshot_exc:
            evidence["screenshot_error"] = str(screenshot_exc)
        try:
            evidence["wda_status"] = device.status()
        except Exception as status_exc:
            evidence["wda_status_error"] = str(status_exc)
        verification: dict[str, Any] = {}
        if callable(getattr(self, "post_action_verifier", None)):
            try:
                verification = self.post_action_verifier(target=self.target, action="tap", x=x, y=y, evidence=evidence) or {}
            except Exception as verify_exc:
                verification = {"ok": False, "error": str(verify_exc)}
        if verification.get("ok"):
            return {
                "ok": True,
                "strategy": "tap_timeout_verified",
                "x": x,
                "y": y,
                "response_timeout": True,
                "verified": True,
                "post_check": evidence,
                "verification": verification,
                "detail": str(exc),
            }
        return {
            "ok": False,
            "strategy": "wda_xy_timeout_postcheck",
            "x": x,
            "y": y,
            "response_timeout": True,
            "verified": False,
            "post_check": evidence,
            "reason": "wda_tap_response_timeout",
            "warning": "WDA tap 请求响应超时，动作可能已在设备生效；请继续使用页面断言或截图差异确认目标状态。",
            "detail": str(exc),
        }

    def _harmony_tap_timeout_result(self, device: Any, x: int, y: int, exc: HDCCommandError) -> dict[str, Any]:
        """Handle harmony tap timeout result using the supplied state and inputs."""
        evidence: dict[str, Any] = {}
        screenshot_path = Path(self.evidence_dir) / f"harmony_tap_timeout_{int(time.time() * 1000)}.png"
        try:
            evidence["screenshot"] = device.screenshot(screenshot_path)
        except Exception as screenshot_exc:
            evidence["screenshot_error"] = str(screenshot_exc)
        failure = build_failure(
            exc.code,
            self.target,
            "tap",
            evidence={"command": exc.command, "detail": exc.detail, **evidence},
        )
        verification: dict[str, Any] = {}
        if callable(getattr(self, "post_action_verifier", None)):
            try:
                verification = self.post_action_verifier(
                    target=self.target,
                    action="tap",
                    x=x,
                    y=y,
                    evidence=evidence,
                ) or {}
            except Exception as verify_exc:
                verification = {"ok": False, "error": str(verify_exc)}
        return {
            "ok": bool(verification.get("ok")),
            "verified": bool(verification.get("ok")),
            "strategy": "hdc_xy_timeout_verified" if verification.get("ok") else "hdc_xy_timeout_postcheck",
            "x": x,
            "y": y,
            "response_timeout": True,
            "reason": "harmony_tap_response_timeout",
            "post_check": evidence,
            "failure": failure,
            **({"verification": verification} if verification else {}),
        }

    def _adb(self, args: list[str], text: bool = True, check: bool = True) -> str | bytes:
        """Execute one bounded ADB command for the active Android device."""
        cmd = ["adb"]
        if self.device_serial:
            cmd += ["-s", self.device_serial]
        cmd += args
        try:
            return subprocess.check_output(cmd, text=text, stderr=subprocess.DEVNULL, timeout=10)
        except subprocess.SubprocessError:
            if check:
                raise
            return "" if text else b""


def _locator_from_step(step: dict[str, Any]) -> dict[str, Any]:
    """Handle locator from step using the supplied state and inputs."""
    return {
        key: step.get(key)
        for key in ("text", "text_contains", "any_text", "resource_id", "resourceId", "content_desc", "bounds", "x", "y")
        if step.get(key)
    }


def _command_visual_locator(command: str):
    """Adapt the configured generic screenshot locator command to DeviceDriver."""

    def locate(*, image_path: str, locator: dict[str, Any], target: str) -> list[dict[str, Any]]:
        """Run the configured visual locator and validate its result."""
        target_text = str(locator.get("text") or locator.get("text_contains") or "")
        target_description = json.dumps(locator, ensure_ascii=False, sort_keys=True)
        env = os.environ.copy()
        env.update(
            {
                "MOBILE_AUTO_MCP_SCREENSHOT": image_path,
                "MOBILE_AUTO_MCP_TARGET_TEXT": target_text,
                "MOBILE_AUTO_MCP_TARGET_DESCRIPTION": target_description,
                "MOBILE_AUTO_MCP_TARGET": target,
            }
        )
        completed = run_configured_command(
            command,
            values={"screenshot": image_path, "text": target_text, "target_description": target_description, "target": target},
            timeout=20,
            env=env,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"visual locator failed: exit={completed.returncode}, stderr={completed.stderr[-1000:]}")
        payload = json.loads(completed.stdout)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        candidates = payload.get("candidates") if isinstance(payload, dict) else None
        if isinstance(candidates, list):
            return [item for item in candidates if isinstance(item, dict)]
        if isinstance(payload, dict) and "x" in payload and "y" in payload:
            x, y = int(payload["x"]), int(payload["y"])
            return [
                {
                    "text": target_text,
                    "bounds": f"[{x},{y}][{x},{y}]",
                    "confidence": float(payload.get("confidence") or 0),
                    "visual": payload,
                }
            ]
        return []

    return locate


def _select_element(elements: list[dict[str, Any]], locator: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Select element using the supplied state and inputs."""
    if not isinstance(locator, dict):
        locator = {"text": str(locator)}
    candidates: list[dict[str, Any]] = []
    for element in elements:
        score = _match_score(element, locator)
        if score <= 0:
            continue
        item = dict(element)
        item["_confidence"] = score
        candidates.append(item)
    candidates.sort(key=lambda item: (int(item.get("_confidence") or 0), bool(item.get("clickable")), bool(item.get("enabled", True))), reverse=True)
    return (candidates[0] if candidates else None, candidates)


def _select_visual_candidate(
    rows: list[dict[str, Any]],
    locator: dict[str, Any],
    image_path: str,
    *,
    min_confidence: float,
    ambiguity_margin: float,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
    """Apply confidence, ambiguity, and screenshot-bound gates to visual candidates."""
    _, semantic = _select_element(rows, locator)
    candidates = semantic or rows
    candidates = sorted(candidates, key=lambda row: float(row.get("confidence") or 0), reverse=True)
    if not candidates:
        return None, [], "visual_locator_unavailable"
    top_confidence = float(candidates[0].get("confidence") or 0)
    if top_confidence < min_confidence:
        return None, candidates, "visual_low_confidence"
    if len(candidates) > 1:
        second_confidence = float(candidates[1].get("confidence") or 0)
        if top_confidence - second_confidence < ambiguity_margin and candidates[0].get("bounds") != candidates[1].get("bounds"):
            return None, candidates, "visual_ambiguous"
    bounds = _bounds_rect(str(candidates[0].get("bounds") or ""))
    if not bounds:
        return None, candidates, "visual_coordinate_out_of_bounds"
    left, top, right, bottom = bounds
    if left < 0 or top < 0 or right < left or bottom < top:
        return None, candidates, "visual_coordinate_out_of_bounds"
    try:
        from PIL import Image

        with Image.open(image_path) as image:
            width, height = image.size
        if right > width or bottom > height:
            return None, candidates, "visual_coordinate_out_of_bounds"
    except Exception:
        pass
    return candidates[0], candidates, ""


def _match_score(element: dict[str, Any], locator: dict[str, Any]) -> int:
    """Score a live element against semantic attributes or an exact live-tree bounds fallback."""
    score = 0
    text = str(element.get("text") or "")
    resource_id = str(element.get("resource_id") or "")
    content_desc = str(element.get("content_desc") or "")
    expected_id = str(locator.get("resource_id") or locator.get("resourceId") or "")
    expected_bounds = str(locator.get("bounds") or "")
    if expected_bounds and str(element.get("bounds") or "") == expected_bounds:
        score = max(score, 96)
    elif expected_bounds:
        return 0
    if expected_id and resource_id == expected_id:
        score = max(score, 100)
    elif expected_id and expected_id in resource_id:
        score = max(score, 88)
    elif expected_id:
        return 0
    expected_text = str(locator.get("text") or "")
    if expected_text and text == expected_text:
        score = max(score, 95)
    elif expected_text and expected_text in text:
        score = max(score, 82)
    elif expected_text:
        return 0
    text_contains = str(locator.get("text_contains") or "")
    if text_contains and text_contains in text:
        score = max(score, 84)
    elif text_contains:
        return 0
    options = [str(option or "") for option in locator.get("any_text") or [] if str(option or "")]
    if options:
        option_scores = [94 if option == text else 86 for option in options if option == text or option in text]
        if not option_scores:
            return 0
        score = max(score, max(option_scores))
    expected_desc = str(locator.get("content_desc") or "")
    if expected_desc and expected_desc == content_desc:
        score = max(score, 90)
    elif expected_desc and expected_desc in content_desc:
        score = max(score, 78)
    elif expected_desc:
        return 0
    return score


def _selected_proxy_mode(elements: list[dict[str, Any]]) -> str:
    """Map the selected localized HTTP proxy mode element to a stable mode name."""
    labels = {
        "none": {"关闭", "无", "Off", "None"},
        "manual": {"手动", "Manual"},
        "auto": {"自动", "Auto", "Automatic"},
    }
    for element in elements:
        if not element.get("selected"):
            continue
        text = str(element.get("text") or "").strip()
        for mode, options in labels.items():
            if text in options:
                return mode
    return ""


def _proxy_text_fields(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return visible editable system-settings fields in screen order for proxy host, port, or URL."""
    editable_markers = ("textfield", "textinput", "edittext", "input")
    fields = [
        item
        for item in elements
        if any(marker in str(item.get("class") or "").lower() for marker in editable_markers)
        and item.get("enabled", True)
        and item.get("bounds")
    ]
    return sorted(fields, key=lambda item: (_bounds_rect(str(item.get("bounds") or "")) or (0, 0, 0, 0))[1])


def _wifi_device_ipv4(elements: list[dict[str, Any]]) -> str:
    """Return the private IPv4 value associated with a localized Wi-Fi address label."""
    labels = ("ip address", "ipv4 address", "ip 地址", "ip地址", "ipv4 地址", "本机地址")
    texts = [str(item.get("text") or item.get("value") or "").strip() for item in elements]
    label_indexes = [index for index, text in enumerate(texts) if any(label in text.lower() for label in labels)]
    if not label_indexes:
        return ""
    # System settings exposes the label before its value in the accessibility tree; bound the search
    # so DNS, router, and proxy server addresses elsewhere on the page cannot become route proof.
    for label_index in label_indexes:
        for text in texts[label_index : label_index + 4]:
            for candidate in re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", text):
                try:
                    address = ipaddress.ip_address(candidate)
                except ValueError:
                    continue
                if address.version == 4 and address.is_private and not address.is_loopback and not address.is_link_local:
                    return str(address)
    return ""


def _wifi_device_interface(elements: list[dict[str, Any]]) -> str:
    """Return the Wi-Fi IPv4 plus a CIDR prefix when the settings page exposes a subnet mask."""
    address = _wifi_device_ipv4(elements)
    if not address:
        return ""
    labels = ("subnet mask", "子网掩码", "子网遮罩")
    texts = [str(item.get("text") or item.get("value") or "").strip() for item in elements]
    for index, text in enumerate(texts):
        if not any(label in text.lower() for label in labels):
            continue
        for candidate_text in texts[index : index + 4]:
            for candidate in re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", candidate_text):
                try:
                    return str(ipaddress.ip_interface(f"{address}/{candidate}"))
                except ValueError:
                    continue
    return address


def _selected_state_matches(element: dict[str, Any], assertion: Any) -> bool:
    """Handle selected state matches using the supplied state and inputs."""
    if not isinstance(assertion, dict) or "selected" not in assertion:
        return True
    return bool(element.get("selected")) is bool(assertion.get("selected"))


def _bounds_rect(bounds: str) -> tuple[int, int, int, int] | None:
    """Parse an Android bounds string into rectangle coordinates."""
    match = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds or "")
    return tuple(int(part) for part in match.groups()) if match else None


def _bounds_center(bounds: str) -> tuple[int | None, int | None]:
    """Return the center coordinates of an Android bounds string."""
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not match:
        return None, None
    x1, y1, x2, y2 = [int(part) for part in match.groups()]
    return (x1 + x2) // 2, (y1 + y2) // 2








def _env_truthy(name: str) -> bool:
    """Return whether an environment variable contains a truthy value."""
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}






