"""Generic click resolution and execution pipeline."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from mobile_auto_mcp.execution.command_runner import run_configured_command
from mobile_auto_mcp.execution.devices import DeviceDriver


def smart_click(
    driver: DeviceDriver,
    *,
    text: str = "",
    resource_id: str = "",
    target_description: str = "",
    x: int | None = None,
    y: int | None = None,
    x_percent: float | None = None,
    y_percent: float | None = None,
    vision_command: str = "",
    allow_tree: bool | None = None,
    allow_wda_fallback: bool = False,
    screenshot_path: str = "",
) -> dict[str, Any]:
    """Resolve a click target, execute it, and return traceable evidence."""
    resolver = ClickResolver(driver)
    resolved = resolver.resolve(
        text=text,
        resource_id=resource_id,
        target_description=target_description,
        x=x,
        y=y,
        x_percent=x_percent,
        y_percent=y_percent,
        vision_command=vision_command,
        allow_tree=allow_tree,
        screenshot_path=screenshot_path,
    )
    if not resolved.get("ok"):
        return {"ok": False, "stage": "resolve", **resolved}

    executor = ClickExecutor(driver)
    executed = executor.execute(
        int(resolved["x"]),
        int(resolved["y"]),
        source=str(resolved.get("strategy") or ""),
        allow_wda_fallback=allow_wda_fallback,
    )
    return {"ok": bool(executed.get("ok")), "resolved": resolved, "executed": executed}


class ClickResolver:
    """Resolve where to tap without coupling to any app."""

    def __init__(self, driver: DeviceDriver) -> None:
        """Initialize ClickResolver state, configuration, and runtime dependencies."""
        self.driver = driver

    def resolve(
        self,
        *,
        text: str,
        resource_id: str,
        target_description: str,
        x: int | None,
        y: int | None,
        x_percent: float | None,
        y_percent: float | None,
        vision_command: str,
        allow_tree: bool | None,
        screenshot_path: str,
    ) -> dict[str, Any]:
        """Resolve the best click target from current UI evidence."""
        if x is not None and y is not None:
            return {"ok": True, "strategy": "explicit_xy", "x": int(x), "y": int(y)}

        if x_percent is not None and y_percent is not None:
            width, height = self._window_size()
            return {"ok": True, "strategy": "percent", "x": int(width * x_percent), "y": int(height * y_percent), "screen": {"width": width, "height": height}}

        tree_allowed = self.driver.target != "ios" if allow_tree is None else bool(allow_tree)
        tree = self._resolve_by_tree(text=text, resource_id=resource_id) if tree_allowed else {"ok": False, "reason": "tree_skipped", "target": self.driver.target}
        if tree.get("ok"):
            return tree

        command = vision_command or os.environ.get("MOBILE_AUTO_MCP_VISION_LOCATOR_COMMAND") or ""
        if command:
            visual = self._resolve_by_vision_command(command=command, text=text, target_description=target_description, screenshot_path=screenshot_path)
            visual["tree_attempt"] = tree
            return visual

        return {
            "ok": False,
            "reason": "vision_locator_required",
            "tree_attempt": tree,
            "vision_contract": {
                "input": "screenshot path plus target text/description",
                "output": {"x": "tap x in device coordinate space", "y": "tap y in device coordinate space", "confidence": "0..1 optional"},
            },
        }

    def _resolve_by_tree(self, *, text: str, resource_id: str) -> dict[str, Any]:
        """Resolve by tree using the supplied state and inputs."""
        if not text and not resource_id:
            return {"ok": False, "reason": "tree_locator_not_provided"}
        try:
            from mobile_auto_mcp.execution.devices import _bounds_center, _select_element

            selected, candidates = _select_element(
                self.driver.list_elements(limit=self.driver.locator_tree_limit),
                {"text": text, "resource_id": resource_id},
            )
        except Exception as exc:
            return {"ok": False, "reason": "tree_unavailable", "error": str(exc)}
        if not selected:
            return {"ok": False, "reason": "element_not_found", "candidates": candidates[:5]}
        x, y = _bounds_center(str(selected.get("bounds") or ""))
        if x is None or y is None:
            return {"ok": False, "reason": "element_has_no_bounds", "selected": selected}
        return {"ok": True, "strategy": "element_tree", "x": x, "y": y, "selected": selected, "candidates": candidates[:5]}

    def _resolve_by_vision_command(self, *, command: str, text: str, target_description: str, screenshot_path: str) -> dict[str, Any]:
        """Resolve by vision command using the supplied state and inputs."""
        screenshot = screenshot_path or str(Path(tempfile.gettempdir()) / "mobile_auto_mcp_smart_click.png")
        self.driver.screenshot(screenshot)
        env = os.environ.copy()
        env.update(
            {
                "MOBILE_AUTO_MCP_SCREENSHOT": screenshot,
                "MOBILE_AUTO_MCP_TARGET_TEXT": text,
                "MOBILE_AUTO_MCP_TARGET_DESCRIPTION": target_description,
            }
        )
        try:
            completed = run_configured_command(
                command,
                values={"screenshot": screenshot, "text": text, "target_description": target_description},
                timeout=20,
                env=env,
            )
        except TimeoutError as exc:
            return {"ok": False, "reason": "vision_command_timeout", "command": command, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "reason": "vision_command_invalid", "command": command, "error": str(exc)}
        if completed.returncode != 0:
            return {"ok": False, "reason": "vision_command_failed", "command": command, "stderr": completed.stderr[-1000:]}
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "reason": "vision_command_non_json", "stdout": completed.stdout[-1000:]}
        if "x" not in payload or "y" not in payload:
            return {"ok": False, "reason": "vision_command_missing_coordinates", "payload": payload}
        return {"ok": True, "strategy": "external_vision_command", "x": int(payload["x"]), "y": int(payload["y"]), "screenshot": screenshot, "vision": payload}

    def _window_size(self) -> tuple[int, int]:
        """Handle window size using the supplied state and inputs."""
        device = self.driver.connect()
        if device is not None and hasattr(device, "window_size"):
            width, height = device.window_size()
            if width and height:
                return int(width), int(height)
        screenshot = Path(tempfile.gettempdir()) / "mobile_auto_mcp_size.png"
        self.driver.screenshot(screenshot)
        return _png_size(screenshot)


class ClickExecutor:
    """Execute a resolved tap with target-specific safety rules."""

    def __init__(self, driver: DeviceDriver) -> None:
        """Initialize ClickExecutor state, configuration, and runtime dependencies."""
        self.driver = driver

    def execute(self, x: int, y: int, *, source: str, allow_wda_fallback: bool) -> dict[str, Any]:
        """Resolve and execute the requested click instruction."""
        if self.driver.target != "ios":
            return self.driver.click(x=x, y=y)
        device = self.driver.connect()
        has_external = bool(getattr(device, "tap_command", ""))
        if has_external:
            return device.tap(x, y)
        if allow_wda_fallback:
            return device.tap(x, y)
        return {
            "ok": False,
            "reason": "ios_external_tap_backend_required",
            "source": source,
            "x": x,
            "y": y,
            "how_to_fix": "配置 MOBILE_AUTO_MCP_IOS_TAP_COMMAND，或显式传 allow_wda_fallback=True 接受 WDA snapshot 风险。",
        }


def _png_size(path: str | Path) -> tuple[int, int]:
    """Read PNG dimensions without decoding the full image."""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
        return 0, 0
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
