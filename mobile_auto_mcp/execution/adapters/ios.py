"""iOS WebDriverAgent adapter and transport-specific errors."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import socket
import subprocess
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from mobile_auto_mcp.execution.command_runner import run_configured_command

DEFAULT_WDA_URL = "http://127.0.0.1:8100"


class WDAConnectionError(RuntimeError):
    """Raised when WebDriverAgent cannot be reached or returns an error."""


class WDAReadinessError(WDAConnectionError):
    """Describe the exact WDA readiness stage that blocked iOS automation."""

    def __init__(self, stage: str, message: str) -> None:
        """Attach a stable stage so preflight can return actionable trust diagnostics."""
        super().__init__(message)
        self.stage = stage


class WDARequestTimeoutError(WDAConnectionError):
    """Raised when a WDA HTTP request times out before returning a response."""


class WDATapTimeoutError(WDARequestTimeoutError):
    """Raised when WDA tap may have executed but its HTTP response timed out."""


class IOSWDAClient:
    """Minimal WebDriverAgent HTTP client used by the iOS device facade."""

    def __init__(self, wda_url: str = "", timeout: float = 6, device_udid: str = "", tap_backend: str = "", tap_command: str = "") -> None:
        """Initialize IOSWDAClient state, configuration, and runtime dependencies."""
        self.wda_url = _validated_wda_url(
            wda_url or os.environ.get("MOBILE_AUTO_MCP_WDA_URL") or os.environ.get("WDA_URL") or DEFAULT_WDA_URL
        )
        self.timeout = timeout
        self.device_udid = device_udid or os.environ.get("MOBILE_AUTO_MCP_IOS_UDID") or ""
        self.tap_backend = (tap_backend or os.environ.get("MOBILE_AUTO_MCP_IOS_TAP_BACKEND") or "auto").lower()
        self.tap_command = tap_command or os.environ.get("MOBILE_AUTO_MCP_IOS_TAP_COMMAND") or ""
        self.session_id = ""

    def status(self) -> dict[str, Any]:
        """Return the current backend readiness status."""
        return self._request("GET", "/status")

    def verify_readiness(self) -> dict[str, Any]:
        """Prove WDA status, session creation, and a read-only XCTest command before automation."""
        try:
            status = self.status()
        except WDAConnectionError as exc:
            raise WDAReadinessError("transport", f"WDA /status 不可用：{exc}") from exc

        value = _wda_value(status)
        status_value = value if isinstance(value, dict) else {}
        ready = status_value.get("ready")
        state = str(status_value.get("state") or "").lower()
        legacy_status = status.get("status") if isinstance(status, dict) else None
        if ready is False:
            message = str(status_value.get("message") or "WebDriverAgent 未准备好")
            raise WDAReadinessError(
                "status",
                f"WDA /status 返回 ready=false：{message}。请确认真机开发者模式和开发者 App 信任已通过。",
            )
        status_ready = ready is True or state == "success" or legacy_status == 0
        if not status_ready:
            raise WDAReadinessError(
                "status",
                "WDA /status 未返回 ready=true 或 success，不能进入后续设备流程。",
            )

        existing_session = str(
            (status.get("sessionId") if isinstance(status, dict) else "")
            or status_value.get("sessionId")
            or self.session_id
            or ""
        )
        previous_session = self.session_id
        temporary_session = False
        if existing_session:
            self.session_id = existing_session
        else:
            try:
                self._create_session()
                temporary_session = True
            except WDAConnectionError as exc:
                raise WDAReadinessError(
                    "session",
                    f"WDA /status 可访问，但 session 创建失败：{exc}。请确认真机已信任开发者并保持 WDA Runner 前台可执行。",
                ) from exc

        probed_session = self.session_id
        try:
            width, height = self.window_size()
        except WDAConnectionError as exc:
            self._cleanup_readiness_session(temporary_session, previous_session)
            raise WDAReadinessError(
                "command",
                f"WDA session 已建立，但基础只读动作 window size 失败：{exc}。",
            ) from exc
        if width <= 0 or height <= 0:
            self._cleanup_readiness_session(temporary_session, previous_session)
            raise WDAReadinessError(
                "command",
                f"WDA window size 返回无效尺寸 {width}x{height}，禁止进入后续设备流程。",
            )

        deleted = self._cleanup_readiness_session(temporary_session, previous_session)
        return {
            "ok": True,
            "stage": "ready",
            "checks": {
                "status_ready": True,
                "status": status,
                "session": {
                    "session_id": probed_session,
                    "temporary": temporary_session,
                },
                "window_size": {"width": width, "height": height},
                "temporary_session_deleted": deleted,
            },
        }

    def _cleanup_readiness_session(self, temporary: bool, previous_session: str) -> bool:
        """Delete only a probe-created session and restore the caller's prior session identity."""
        if not temporary:
            return False
        probe_session = self.session_id
        try:
            self._request("DELETE", f"/session/{probe_session}")
        except WDAConnectionError as exc:
            self.session_id = previous_session
            raise WDAReadinessError(
                "session_cleanup",
                f"WDA 临时 readiness session 清理失败：{exc}",
            ) from exc
        self.session_id = previous_session
        return True

    def launch_app(self, bundle_id: str) -> dict[str, Any]:
        """Launch the requested application package or bundle."""
        if not bundle_id:
            raise ValueError("iOS target_app_package/bundle_id 不能为空")
        try:
            response = self._request("POST", "/wda/apps/launchUnattached", {"bundleId": bundle_id})
            return {"ok": True, "bundleId": bundle_id, "strategy": "wda_launch_unattached", "response": response}
        except WDAConnectionError:
            session = self._create_session()
            self._request("POST", f"/session/{self.session_id}/wda/apps/launch", {"bundleId": bundle_id})
            return {"ok": True, "bundleId": bundle_id, "strategy": "wda_session_launch", "session_id": self.session_id, "session": session}

    def current_app(self) -> dict[str, Any]:
        """Return the application currently in the foreground."""
        if self.session_id:
            try:
                payload = self._request("GET", f"/session/{self.session_id}/wda/activeAppInfo")
                return _wda_value(payload)
            except WDAConnectionError:
                pass
        return {"wda": self.status()}

    def list_elements(self, limit: int = 80) -> list[dict[str, Any]]:
        """Return a bounded snapshot of visible UI elements."""
        self._ensure_session()
        payload = self._request("GET", f"/session/{self.session_id}/source")
        source = str(_wda_value(payload) or "")
        if not source.strip():
            return []
        rows = _elements_from_wda_xml(source)
        return rows[:limit]

    def tap(self, x: int, y: int) -> dict[str, Any]:
        """Tap the requested device coordinates."""
        if self.tap_backend not in {"auto", "actions", "wda", "external"}:
            raise ValueError("ios_tap_backend 仅支持 auto、actions、wda、external")
        external_error = ""
        if self.tap_backend in {"auto", "external"} and self.tap_command:
            try:
                return self._external_tap(int(x), int(y))
            except WDAConnectionError as exc:
                external_error = str(exc)
                if self.tap_backend == "external":
                    raise
        if self.tap_backend == "external":
            raise WDAConnectionError("iOS external 点击后端未配置：请设置 ios_tap_command 或 MOBILE_AUTO_MCP_IOS_TAP_COMMAND")

        actions_error = ""
        if self.tap_backend in {"auto", "actions"}:
            try:
                result = self._actions_tap(int(x), int(y))
                if external_error:
                    result["external_tap_error"] = external_error
                return result
            except WDARequestTimeoutError as exc:
                raise WDATapTimeoutError(
                    "iOS W3C Actions 点击响应超时。动作可能已经发送到设备，为避免重复点击不会回退到 /wda/tap；"
                    f"调用方应通过截图或页面断言确认最终状态。detail={exc}"
                ) from exc
            except WDAConnectionError as exc:
                actions_error = str(exc)
                if self.tap_backend == "actions":
                    raise WDAConnectionError(f"iOS W3C Actions 点击失败。detail={actions_error}") from exc

        self._ensure_session()
        try:
            self._request("POST", f"/session/{self.session_id}/wda/tap", {"x": int(x), "y": int(y)})
        except WDARequestTimeoutError as exc:
            detail = str(exc)
            if external_error:
                detail = f"{detail}; external_tap_error={external_error}"
            if actions_error:
                detail = f"{detail}; actions_error={actions_error}"
            raise WDATapTimeoutError(
                f"iOS 点击响应超时。WDA 可能已经把 tap 发送到设备，但 XCTest 未在超时时间内返回；"
                f"调用方应通过截图或页面断言确认最终状态。detail={detail}"
            ) from exc
        except WDAConnectionError as exc:
            detail = str(exc)
            if external_error:
                detail = f"{detail}; external_tap_error={external_error}"
            if actions_error:
                detail = f"{detail}; actions_error={actions_error}"
            raise WDAConnectionError(
                f"iOS 点击失败。W3C Actions 与兼容 /wda/tap 均不可用；"
                f"可配置通用原生点击后端 ios_tap_command 后重试。detail={detail}"
            ) from exc
        result = {
            "ok": True,
            "strategy": "wda_xy_fallback" if actions_error else "wda_xy",
            "x": int(x),
            "y": int(y),
        }
        if external_error:
            result["external_tap_error"] = external_error
        if actions_error:
            result["actions_error"] = actions_error
        return result

    def _actions_tap(self, x: int, y: int) -> dict[str, Any]:
        """Handle actions tap using the supplied state and inputs."""
        self._ensure_session()
        self._request(
            "POST",
            f"/session/{self.session_id}/actions",
            {
                "actions": [
                    {
                        "type": "pointer",
                        "id": "finger1",
                        "parameters": {"pointerType": "touch"},
                        "actions": [
                            {"type": "pointerMove", "duration": 0, "x": x, "y": y},
                            {"type": "pointerDown", "button": 0},
                            {"type": "pause", "duration": 100},
                            {"type": "pointerUp", "button": 0},
                        ],
                    }
                ]
            },
        )
        return {"ok": True, "strategy": "wda_actions", "x": x, "y": y}

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.2) -> dict[str, Any]:
        """Swipe between the requested device coordinates."""
        self._ensure_session()
        self._request(
            "POST",
            f"/session/{self.session_id}/wda/dragfromtoforduration",
            {"fromX": int(x1), "fromY": int(y1), "toX": int(x2), "toY": int(y2), "duration": float(duration)},
        )
        return {"ok": True, "strategy": "wda_swipe"}

    def window_size(self) -> tuple[int, int]:
        """Return the current device viewport dimensions."""
        self._ensure_session()
        try:
            payload = self._request("GET", f"/session/{self.session_id}/window/size")
            value = _wda_value(payload) or {}
            return int(value.get("width") or 0), int(value.get("height") or 0)
        except WDAConnectionError:
            payload = self._request("GET", f"/session/{self.session_id}/window/rect")
            value = _wda_value(payload) or {}
            return int(value.get("width") or 0), int(value.get("height") or 0)

    def screenshot(self, path: str | Path) -> str:
        """Capture a device screenshot at the requested path."""
        payload = self._request("GET", "/screenshot")
        encoded = str(_wda_value(payload) or "")
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(encoded) if encoded else b"")
        return str(out)

    def input_text(self, text: str, clear: bool = True) -> dict[str, Any]:
        """Type into the currently focused iOS field using WDA keyboard endpoints."""
        self._ensure_session()
        values = (["\b"] * 128 if clear else []) + list(str(text))
        try:
            self._request("POST", f"/session/{self.session_id}/wda/keys", {"value": values})
            strategy = "wda_keys"
        except WDAConnectionError:
            self._request("POST", f"/session/{self.session_id}/keys", {"value": values})
            strategy = "webdriver_keys"
        return {"ok": True, "strategy": strategy, "length": len(str(text))}

    def _create_session(self, bundle_id: str = "") -> dict[str, Any]:
        """Create session using the supplied state and inputs."""
        capabilities = {
            "platformName": "iOS",
            "shouldWaitForQuiescence": False,
            "waitForIdleTimeout": 0,
        }
        if bundle_id:
            capabilities["bundleId"] = bundle_id
        payload = self._request(
            "POST",
            "/session",
            {
                "capabilities": {"alwaysMatch": capabilities, "firstMatch": [{}]},
                "desiredCapabilities": capabilities,
            },
        )
        self.session_id = str(payload.get("sessionId") or (payload.get("value") or {}).get("sessionId") or self.session_id)
        if not self.session_id:
            raise WDAConnectionError("WDA 未返回 sessionId")
        return payload

    def _ensure_session(self) -> None:
        """Ensure session using the supplied state and inputs."""
        if not self.session_id:
            self._create_session()

    def reset_session(self) -> dict[str, Any]:
        """Discard a stale WDA session without reinstalling or rebuilding WDA."""
        previous = self.session_id
        if previous:
            try:
                self._request("DELETE", f"/session/{previous}")
            except WDAConnectionError:
                pass
        self.session_id = ""
        payload = self._create_session()
        return {"ok": True, "action": "recreate_session", "previous_session_id": previous, "session_id": self.session_id, "response": payload}

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one bounded HTTP request to WebDriverAgent."""
        url = self.wda_url + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            # `_validated_wda_url` limits requests to the HTTP transports supported by WDA.
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # nosec B310
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise WDAConnectionError(f"WDA 请求失败: {method} {url}: {exc}") from exc
        except urllib.error.URLError as exc:
            if _is_timeout_exception(exc):
                raise WDARequestTimeoutError(f"WDA 请求超时: {method} {url}: {exc}") from exc
            raise WDAConnectionError(f"WDA 请求失败: {method} {url}: {exc}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise WDARequestTimeoutError(f"WDA 请求超时: {method} {url}: {exc}") from exc
        except OSError as exc:
            if _is_timeout_exception(exc):
                raise WDARequestTimeoutError(f"WDA 请求超时: {method} {url}: {exc}") from exc
            raise WDAConnectionError(f"WDA 请求失败: {method} {url}: {exc}") from exc
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WDAConnectionError(f"WDA 返回非 JSON: {method} {url}") from exc
        if isinstance(payload, dict) and payload.get("status") not in (None, 0):
            raise WDAConnectionError(f"WDA 返回错误: {payload}")
        return payload

    def _external_tap(self, x: int, y: int) -> dict[str, Any]:
        """Handle external tap using the supplied state and inputs."""
        env = os.environ.copy()
        env.update(
            {
                "MOBILE_AUTO_MCP_TAP_X": str(x),
                "MOBILE_AUTO_MCP_TAP_Y": str(y),
                "MOBILE_AUTO_MCP_IOS_UDID": self.device_udid,
                "MOBILE_AUTO_MCP_WDA_URL": self.wda_url,
            }
        )
        try:
            completed = run_configured_command(
                self.tap_command,
                values={"x": x, "y": y, "device_udid": self.device_udid, "udid": self.device_udid, "wda_url": self.wda_url},
                timeout=self.timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise WDAConnectionError(f"external iOS 点击命令超时: {self.tap_command}") from exc
        except ValueError as exc:
            raise WDAConnectionError(f"external iOS 点击命令配置无效: {exc}") from exc
        if completed.returncode != 0:
            stderr = (completed.stderr or completed.stdout or "").strip()
            raise WDAConnectionError(f"external iOS 点击命令失败: exit={completed.returncode}, stderr={stderr}")
        return {
            "ok": True,
            "strategy": "external_ios_tap",
            "x": x,
            "y": y,
            "stdout": (completed.stdout or "").strip()[-1000:],
        }


def _wda_value(payload: dict[str, Any]) -> Any:
    """Handle wda value using the supplied state and inputs."""
    return payload.get("value") if isinstance(payload, dict) and "value" in payload else payload


def _elements_from_wda_xml(source: str) -> list[dict[str, Any]]:
    """Handle elements from wda xml using the supplied state and inputs."""
    if "<!DOCTYPE" in source.upper() or "<!ENTITY" in source.upper():
        return []
    try:
        # Declarations and entities are rejected above before parsing.
        root = ET.fromstring(source)  # nosec B314
    except ET.ParseError:
        return []
    rows: list[dict[str, Any]] = []
    for node in root.iter():
        attrs = node.attrib
        text = attrs.get("label") or attrs.get("name") or attrs.get("value") or ""
        x = _int_attr(attrs, "x")
        y = _int_attr(attrs, "y")
        width = _int_attr(attrs, "width")
        height = _int_attr(attrs, "height")
        bounds = f"[{x},{y}][{x + width},{y + height}]" if width or height else ""
        rows.append(
            {
                "text": str(text),
                "resource_id": attrs.get("name") or attrs.get("identifier") or "",
                "content_desc": attrs.get("label") or "",
                "class": attrs.get("type") or node.tag,
                "clickable": attrs.get("enabled", "true") != "false",
                "selected": attrs.get("selected") == "true",
                "enabled": attrs.get("enabled", "true") != "false",
                "bounds": bounds,
            }
        )
    return rows


def _int_attr(attrs: dict[str, str], key: str) -> int:
    """Parse an integer XML attribute, returning zero when invalid."""
    try:
        return int(float(attrs.get(key) or 0))
    except ValueError:
        return 0


def _is_timeout_exception(exc: BaseException) -> bool:
    """Return whether an exception chain represents a timeout."""
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException) and _is_timeout_exception(reason):
        return True
    return "timed out" in str(exc).lower() or "timeout" in str(exc).lower()


def _validated_wda_url(value: str) -> str:
    """Accept only absolute HTTP(S) WDA endpoints and normalize their trailing slash."""
    normalized = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("WDA URL 必须是包含主机名的 http:// 或 https:// 地址")
    return normalized


def probe_wda_readiness(
    wda_url: str = "",
    *,
    timeout: float = 6,
    device_udid: str = "",
) -> dict[str, Any]:
    """Return structured strong-readiness evidence for preflight and WDA guardian."""
    try:
        return IOSWDAClient(wda_url, timeout=timeout, device_udid=device_udid).verify_readiness()
    except WDAReadinessError as exc:
        return {"ok": False, "stage": exc.stage, "error": str(exc)}
    except WDAConnectionError as exc:
        return {"ok": False, "stage": "transport", "error": str(exc)}


def probe_wda_transport(wda_url: str = "", *, timeout: float = 6) -> dict[str, Any]:
    """Check only the WDA HTTP transport for keepalive loops that must not churn sessions."""
    try:
        return {"ok": True, "stage": "transport", "status": IOSWDAClient(wda_url, timeout=timeout).status()}
    except WDAConnectionError as exc:
        return {"ok": False, "stage": "transport", "error": str(exc)}
