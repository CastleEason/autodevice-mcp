"""Regression tests for extracting fresh device Wi-Fi addresses before proxy selection."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from mobile_auto_mcp.execution.devices import DeviceDriver


def _load_parser() -> Callable[[str], str]:
    """Load the intended parser seam so a missing network proof remains an explicit Red failure."""
    try:
        module = importlib.import_module("mobile_auto_mcp.proxy.host_selection")
    except ModuleNotFoundError:
        pytest.fail("缺少 mobile_auto_mcp.proxy.host_selection")
    parser = getattr(module, "parse_device_wifi_ip", None)
    assert callable(parser), "缺少 parse_device_wifi_ip(output)"
    return parser


def _load_discovery() -> Callable[..., dict[str, Any]]:
    """Load the device discovery seam that supplies fresh Wi-Fi proof to the Runner."""
    module = importlib.import_module("mobile_auto_mcp.proxy.host_selection")
    discovery = getattr(module, "discover_device_wifi_ip", None)
    assert callable(discovery), "缺少 discover_device_wifi_ip(target, device_serial, driver, command_runner)"
    return discovery


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("wlan0: inet 192.168.50.21/24 brd 192.168.50.255", "192.168.50.21"),
        ("wlan0 inet addr:10.77.3.209 Bcast:10.77.3.255 Mask:255.255.255.0", "10.77.3.209"),
        ("lo inet addr:127.0.0.1 Mask:255.0.0.0", ""),
    ],
)
def test_parse_device_wifi_ip_supports_android_and_harmony_formats(output: str, expected: str) -> None:
    """验证 Android/Harmony 的常见 ifconfig 输出可转为安全 Host 选择所需的新鲜 Wi-Fi IP。"""
    assert _load_parser()(output) == expected


def test_discover_harmony_wifi_ip_uses_the_selected_device() -> None:
    """验证多台 HarmonyOS 设备时命令携带 serial，避免读取另一台手机的 WLAN 地址。"""
    calls: list[list[str]] = []

    def command_runner(command: list[str]) -> SimpleNamespace:
        """Return isolated HDC output while recording the exact device-scoped command."""
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="wlan0 inet addr:10.77.3.209 Mask:255.255.255.0", stderr="")

    result = _load_discovery()("harmony", "device-h", None, command_runner=command_runner)

    assert result["ok"] is True
    assert result["device_ip"] == "10.77.3.209"
    assert calls == [["hdc", "-t", "device-h", "shell", "ifconfig", "wlan0"]]


def test_discover_ios_wifi_ip_requires_semantic_driver_readback() -> None:
    """验证 iOS 不猜测 USB/WDA 地址，只接受系统 Wi-Fi 详情页读取到的设备地址。"""
    driver = SimpleNamespace(read_wifi_network_info=lambda: {"ok": True, "device_ip": "192.168.50.22"})

    result = _load_discovery()("ios", "ios-udid", driver)

    assert result == {"ok": True, "target": "ios", "device_ip": "192.168.50.22", "source": "semantic_settings"}


def test_ios_driver_reads_wifi_ip_from_details_without_opening_proxy_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 iOS Host 证明停留在 Wi-Fi 详情页读取地址，不把代理配置页字段误认成设备 IP。"""
    driver = DeviceDriver(target="ios", device_serial="ios-udid")
    monkeypatch.setattr(driver, "_open_wifi_details", lambda: {"ok": True, "ssid": "qa-wifi"}, raising=False)
    monkeypatch.setattr(
        driver,
        "list_elements",
        lambda limit=80: [
            {"text": "IP Address", "bounds": "{{20,100},{120,40}}"},
            {"text": "192.168.50.22", "bounds": "{{200,100},{160,40}}"},
            {"text": "Configure Proxy", "bounds": "{{20,500},{300,40}}"},
        ],
    )

    result = driver.read_wifi_network_info()

    assert result["ok"] is True
    assert result["device_ip"] == "192.168.50.22"
    assert result["ssid"] == "qa-wifi"


def test_ios_driver_preserves_subnet_mask_as_an_interface_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证系统详情页提供子网掩码时将其转换为 CIDR，避免 Host 选择擅自假定 /24。"""
    driver = DeviceDriver(target="ios", device_serial="ios-udid")
    monkeypatch.setattr(driver, "_open_wifi_details", lambda: {"ok": True, "ssid": "qa-wifi"}, raising=False)
    monkeypatch.setattr(
        driver,
        "list_elements",
        lambda limit=80: [
            {"text": "IP Address"},
            {"text": "192.168.50.130"},
            {"text": "Subnet Mask"},
            {"text": "255.255.255.128"},
        ],
    )

    result = driver.read_wifi_network_info()

    assert result["device_ip"] == "192.168.50.130/25"
