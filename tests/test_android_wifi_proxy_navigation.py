"""Regression tests for Android current-Wi-Fi proxy navigation."""

from __future__ import annotations

from typing import Any

from mobile_auto_mcp.execution.devices import DeviceDriver


def test_android_wifi_details_opens_official_wifi_settings_intent_without_global_proxy_write(
    monkeypatch,
) -> None:
    """验证 Android 通过官方 Wi-Fi 设置页进入当前网络详情，且不执行全局代理命令。"""
    driver = DeviceDriver(target="android", device_serial="device-1")
    commands: list[list[str]] = []

    def fake_adb(args: list[str], text: bool = True, check: bool = True) -> str:
        """Record ADB calls and reject any attempted global proxy mutation."""
        commands.append(list(args))
        assert args[:4] != ["shell", "settings", "put", "global"]
        assert args[:4] != ["shell", "settings", "delete", "global"]
        return ""

    monkeypatch.setattr(driver, "_adb", fake_adb)
    monkeypatch.setattr(
        driver,
        "list_elements",
        lambda limit=80: [
            {
                "text": "QA-WiFi",
                "selected": True,
                "enabled": True,
                "clickable": True,
                "bounds": "[0,100][1080,260]",
            }
        ],
    )
    monkeypatch.setattr(
        driver,
        "resolve_and_click",
        lambda step: {
            "ok": True,
            "strategy": "semantic",
            "locator": dict(step),
        },
    )

    result = driver._open_wifi_details()

    assert result["ok"] is True
    assert result["ssid"] == "QA-WiFi"
    assert ["shell", "am", "start", "-a", "android.settings.WIFI_SETTINGS"] in commands
    assert not any("http_proxy" in command or "global_http_proxy" in command for command in commands)


def test_android_proxy_readback_uses_visible_wifi_settings_fields(monkeypatch) -> None:
    """验证 Android 代理复核读取当前 Wi-Fi 页面中的模式、服务器和端口。"""
    driver = DeviceDriver(target="android", device_serial="device-1")
    elements: list[dict[str, Any]] = [
        {"text": "Manual", "selected": True, "enabled": True, "bounds": "[0,0][100,50]"},
        {"text": "192.168.1.20", "class": "android.widget.EditText", "enabled": True, "bounds": "[0,100][500,160]"},
        {"text": "13000", "class": "android.widget.EditText", "enabled": True, "bounds": "[0,180][500,240]"},
    ]
    monkeypatch.setattr(driver, "_open_system_proxy_settings", lambda: {"ok": True, "ssid": "QA-WiFi"})
    monkeypatch.setattr(driver, "list_elements", lambda limit=80: elements)

    result = driver.read_system_proxy()

    assert result["ok"] is True
    assert result["ssid"] == "QA-WiFi"
    assert result["mode"] == "manual"
    assert result["host"] == "192.168.1.20"
    assert result["port"] == 13000
