"""Regression tests for restoring proxies only on the captured Wi-Fi network."""

from __future__ import annotations

import pytest

from mobile_auto_mcp.proxy.device_proxy import (
    ProxySnapshot,
    SemanticSettingsProxyAdapter,
    build_device_proxy_adapter,
)


class _FakeDriver:
    """Expose deterministic semantic proxy reads and record attempted writes."""

    def __init__(self, ssid: str) -> None:
        """Initialize the fake with the currently connected Wi-Fi SSID."""
        self.ssid = ssid
        self.configurations: list[dict[str, object]] = []

    def read_system_proxy(self) -> dict[str, object]:
        """Return the current Wi-Fi proxy state without mutation."""
        return {"ok": True, "ssid": self.ssid, "mode": "manual", "host": "proxy.test", "port": 8080}

    def configure_system_proxy(self, **configuration: object) -> dict[str, object]:
        """Record a configuration only when the adapter reaches the mutation boundary."""
        self.configurations.append(configuration)
        return {"ok": True}


def test_semantic_restore_blocks_when_current_ssid_differs() -> None:
    """验证设备切换 Wi-Fi 后不会把旧网络的代理快照写入当前网络。"""
    driver = _FakeDriver("other-wifi")
    adapter = SemanticSettingsProxyAdapter("ios", "device", driver)
    snapshot = ProxySnapshot(target="ios", device_serial="device", ssid="qa-wifi", mode="none")

    with pytest.raises(RuntimeError, match="SSID"):
        adapter.restore(snapshot)

    assert driver.configurations == []


def test_semantic_snapshot_requires_an_identified_ssid() -> None:
    """验证无法识别当前 Wi-Fi 时禁止创建可绕过网络身份校验的空 SSID 快照。"""
    adapter = SemanticSettingsProxyAdapter("ios", "device", _FakeDriver(""))

    with pytest.raises(RuntimeError, match="SSID"):
        adapter.snapshot()


def test_android_proxy_uses_current_wifi_semantic_driver_instead_of_global_settings() -> None:
    """验证 Android 与其他端一致地修改当前 Wi-Fi，不再写系统全局 HTTP 代理。"""
    driver = _FakeDriver("qa-wifi")
    adapter = build_device_proxy_adapter("android", "android-device", driver)

    applied = adapter.apply("192.168.1.20", 13000)

    assert isinstance(adapter, SemanticSettingsProxyAdapter)
    assert applied["ok"] is True
    assert driver.configurations == [
        {
            "mode": "manual",
            "host": "192.168.1.20",
            "port": 13000,
            "auto_config_url": "",
        }
    ]


def test_android_proxy_refuses_to_fall_back_when_semantic_driver_is_missing() -> None:
    """验证缺少系统设置驱动时直接阻断，不能静默降级到 settings put global。"""
    with pytest.raises(ValueError, match="DeviceDriver"):
        build_device_proxy_adapter("android", "android-device")
