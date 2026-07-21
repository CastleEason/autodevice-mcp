"""HarmonyOS HDC device adapter and layout parsing helpers."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any


class HDCCommandError(RuntimeError):
    """Structured HDC failure used by bounded HarmonyOS recovery."""

    def __init__(self, code: str, command: list[str], detail: str) -> None:
        """Initialize HDCCommandError state, configuration, and runtime dependencies."""
        super().__init__(detail)
        self.code = code
        self.command = command
        self.detail = detail


class HarmonyHDCClient:
    """Small HarmonyOS driver backed by HDC shell commands."""

    def __init__(self, device_serial: str = "", timeout: float = 10) -> None:
        """Initialize HarmonyHDCClient state, configuration, and runtime dependencies."""
        self.device_serial = device_serial
        self.timeout = timeout

    def current_app(self) -> dict[str, Any]:
        """Return the application currently in the foreground."""
        output = self._hdc(
            ["shell", "hidumper", "-s", "AbilityManagerService", "-a", "-l"],
            check=False,
            timeout=min(self.timeout, 3),
        )
        text = output.decode("utf-8", errors="ignore") if isinstance(output, bytes) else str(output)
        foreground_block = _harmony_foreground_block(text)
        bundle = _harmony_field(foreground_block, "bundle name") or _harmony_field(foreground_block, "app name")
        ability = _harmony_field(foreground_block, "main name")
        return {"bundle": bundle, "ability": ability, "raw": foreground_block[-2000:] or text[-2000:]}

    def launch_app(self, bundle_id: str) -> dict[str, Any]:
        """Launch the requested application package or bundle."""
        if not bundle_id:
            raise ValueError("HarmonyOS target_app_package 不能为空；建议传 bundle/ability，例如 com.example.app/EntryAbility")
        bundle, ability = _split_harmony_bundle(bundle_id)
        command = ["shell", "aa", "start", "-b", bundle, "-a", ability or "EntryAbility"]
        output = self._hdc(command, check=False)
        text = output.decode("utf-8", errors="ignore") if isinstance(output, bytes) else str(output)
        ok = "error" not in text.lower() and "fail" not in text.lower()
        return {"ok": ok, "bundle": bundle, "ability": ability or "EntryAbility", "strategy": "hdc_aa_start", "stdout": text[-1000:]}

    def list_elements(self, limit: int = 80) -> list[dict[str, Any]]:
        """Return a bounded snapshot of visible UI elements."""
        remote = "/data/local/tmp/mobile_auto_mcp_layout.json"
        self._hdc(["shell", "uitest", "dumpLayout", "-p", remote], check=False, timeout=min(self.timeout, 3))
        with tempfile.TemporaryDirectory(prefix="mobile_auto_mcp_layout_") as temp_dir:
            self._hdc(["file", "recv", remote, temp_dir], check=False, timeout=min(self.timeout, 3))
            received = Path(temp_dir) / Path(remote).name
            if not received.exists():
                return []
            text = received.read_text(encoding="utf-8", errors="ignore")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        rows: list[dict[str, Any]] = []
        _collect_harmony_elements(payload, rows)
        return rows[:limit]

    def tap(self, x: int, y: int) -> dict[str, Any]:
        """Tap the requested device coordinates."""
        x_text, y_text = str(int(x)), str(int(y))
        uinput_command = ["shell", "uinput", "-T", "-c", x_text, y_text]
        output = self._hdc(uinput_command, check=False, timeout=min(self.timeout, 6))
        text = output.decode("utf-8", errors="ignore") if isinstance(output, bytes) else str(output)
        unavailable = any(marker in text.lower() for marker in ("not found", "unknown command", "illegal option", "usage:"))
        if not unavailable:
            return {
                "ok": _harmony_command_ok(text),
                "strategy": "hdc_uinput_xy",
                "x": int(x),
                "y": int(y),
                "stdout": text[-1000:],
            }
        output = self._hdc(["shell", "uitest", "uiInput", "click", x_text, y_text], check=False)
        text = output.decode("utf-8", errors="ignore") if isinstance(output, bytes) else str(output)
        ok = _harmony_command_ok(text)
        return {"ok": ok, "strategy": "hdc_uitest_xy_fallback", "x": int(x), "y": int(y), "stdout": text[-1000:]}

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.2) -> dict[str, Any]:
        """Swipe between the requested device coordinates."""
        output = self._hdc(
            ["shell", "uitest", "uiInput", "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(duration * 1000))],
            check=False,
        )
        text = output.decode("utf-8", errors="ignore") if isinstance(output, bytes) else str(output)
        ok = _harmony_command_ok(text)
        return {"ok": ok, "strategy": "hdc_swipe", "stdout": text[-1000:]}

    def back(self) -> dict[str, Any]:
        """Navigate back through the platform-native action."""
        command = ["shell", "uitest", "uiInput", "keyEvent", "Back"]
        output = self._hdc(command, check=False)
        text = output.decode("utf-8", errors="ignore") if isinstance(output, bytes) else str(output)
        return {
            "ok": _harmony_command_ok(text),
            "strategy": "hdc_back_key_event",
            "command": ["hdc", *command],
            "stdout": text[-1000:],
        }

    def window_size(self) -> tuple[int, int]:
        """Return the current device viewport dimensions."""
        out = self._hdc(["shell", "hidumper", "-s", "DisplayManagerService"], check=False)
        text = out.decode("utf-8", errors="ignore") if isinstance(out, bytes) else str(out)
        match = re.search(r"(?:width|Width)[:=]\s*(\d+).*?(?:height|Height)[:=]\s*(\d+)", text, re.S)
        if match:
            return int(match.group(1)), int(match.group(2))
        return 0, 0

    def screenshot(self, path: str | Path) -> str:
        """Capture a device screenshot at the requested path."""
        from PIL import Image

        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        remote = "/data/local/tmp/mobile_auto_mcp_screen.jpeg"
        self._hdc(["shell", "snapshot_display", "-f", remote], timeout=max(self.timeout, 30))
        temp_dir = out.parent / f".{out.stem}_hdc_recv"
        temp_dir.mkdir(parents=True, exist_ok=True)
        self._hdc(["file", "recv", remote, str(temp_dir)], timeout=max(self.timeout, 30))
        received = temp_dir / Path(remote).name
        if received.exists():
            if out.suffix.lower() in {".jpg", ".jpeg"}:
                received.replace(out)
            else:
                image_format = {".webp": "WEBP"}.get(out.suffix.lower(), "PNG")
                with Image.open(received) as image:
                    image.save(out, image_format)
                received.unlink()
        elif not out.exists():
            raise FileNotFoundError(f"hdc screenshot did not create {out}")
        try:
            temp_dir.rmdir()
        except OSError:
            pass
        return str(out)

    def input_text(self, text: str, clear: bool = True) -> dict[str, Any]:
        """Replace text in the focused HarmonyOS field through the HDC UiTest input command."""
        if clear:
            self._hdc(["shell", "uitest", "uiInput", "keyEvent", "CTRL_A"], check=False)
            self._hdc(["shell", "uitest", "uiInput", "keyEvent", "DEL"], check=False)
        output = self._hdc(["shell", "uitest", "uiInput", "inputText", str(text)], check=False)
        rendered = output.decode("utf-8", errors="ignore") if isinstance(output, bytes) else str(output)
        return {"ok": _harmony_command_ok(rendered), "strategy": "hdc_uitest_input_text", "stdout": rendered[-1000:]}

    def _hdc(self, args: list[str], check: bool = True, timeout: float | None = None) -> str | bytes:
        """Execute one bounded HDC command with device-aware fallback."""
        base = ["hdc"]
        commands = [base + (["-t", self.device_serial] if self.device_serial else []) + args]
        if self.device_serial:
            commands.append(base + args)
        last_error: Exception | None = None
        for cmd in commands:
            try:
                return subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=timeout or self.timeout)
            except subprocess.TimeoutExpired as exc:
                code = "harmony_layout_timeout" if "dumpLayout" in args else "harmony_command_timeout"
                raise HDCCommandError(code, cmd, f"HDC command timed out after {timeout or self.timeout}s: {' '.join(cmd)}") from exc
            except (OSError, subprocess.SubprocessError) as exc:
                last_error = exc
                output = getattr(exc, "output", b"")
                text = output.decode("utf-8", errors="ignore") if isinstance(output, bytes) else str(output)
                if check and "Device not founded or connected" not in text:
                    raise
        if check and last_error:
            raise last_error
        return getattr(last_error, "output", b"") if last_error else b""


def _split_harmony_bundle(value: str) -> tuple[str, str]:
    """Split harmony bundle using the supplied state and inputs."""
    text = (value or "").strip()
    if "/" not in text:
        return text, ""
    bundle, ability = text.split("/", 1)
    return bundle.strip(), ability.strip()


def _harmony_foreground_block(text: str) -> str:
    """Handle harmony foreground block using the supplied state and inputs."""
    blocks = re.split(r"\n\s*Mission ID #", text or "")
    for block in blocks:
        if "state #FOREGROUND" in block or "app state #FOREGROUND" in block:
            return block
    return ""


def _harmony_field(block: str, name: str) -> str:
    """Handle harmony field using the supplied state and inputs."""
    match = re.search(rf"{re.escape(name)}\s+\[([^\]]*)\]", block or "")
    return match.group(1).strip() if match else ""


def _collect_harmony_elements(value: Any, rows: list[dict[str, Any]]) -> None:
    """Collect harmony elements using the supplied state and inputs."""
    if isinstance(value, dict):
        text = str(value.get("text") or value.get("value") or value.get("description") or "")
        bounds = _harmony_bounds(value)
        if text or bounds:
            rows.append(
                {
                    "text": text,
                    "resource_id": str(value.get("id") or value.get("key") or ""),
                    "content_desc": str(value.get("description") or ""),
                    "class": str(value.get("type") or value.get("class") or ""),
                    "clickable": bool(value.get("clickable", True)),
                    "selected": bool(value.get("selected", False)),
                    "enabled": bool(value.get("enabled", True)),
                    "bounds": bounds,
                }
            )
        for child in value.values():
            _collect_harmony_elements(child, rows)
    elif isinstance(value, list):
        for item in value:
            _collect_harmony_elements(item, rows)


def _harmony_bounds(value: dict[str, Any]) -> str:
    """Handle harmony bounds using the supplied state and inputs."""
    bounds = value.get("bounds") or value.get("rect")
    if isinstance(bounds, str) and bounds:
        return bounds
    if isinstance(bounds, dict):
        left = int(bounds.get("left") or bounds.get("x") or 0)
        top = int(bounds.get("top") or bounds.get("y") or 0)
        right = int(bounds.get("right") or (left + int(bounds.get("width") or 0)))
        bottom = int(bounds.get("bottom") or (top + int(bounds.get("height") or 0)))
        return f"[{left},{top}][{right},{bottom}]"
    return ""


def _harmony_command_ok(text: str) -> bool:
    """Handle harmony command ok using the supplied state and inputs."""
    lowered = (text or "").lower()
    if "no error" in lowered or "success" in lowered:
        return True
    return "error" not in lowered and "fail" not in lowered
