"""Regression tests for safe, device-aware proxy host selection."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

import pytest


def _load_selector() -> Callable[..., dict[str, Any]]:
    """Load the intended pure selection seam and turn a missing Red API into an assertion failure."""
    try:
        module = importlib.import_module("mobile_auto_mcp.proxy.host_selection")
    except ModuleNotFoundError:
        pytest.fail("缺少 mobile_auto_mcp.proxy.host_selection 安全选择模块")
    selector = getattr(module, "select_proxy_host", None)
    assert callable(selector), "缺少 select_proxy_host(candidates, explicit_host, device_wifi_ips)"
    return selector


def _load_reachability_probe() -> Callable[..., dict[str, Any]]:
    """Load the host-to-device route proof seam used before mitmproxy startup."""
    module = importlib.import_module("mobile_auto_mcp.proxy.host_selection")
    probe = getattr(module, "probe_proxy_host_reachability", None)
    assert callable(probe), "缺少 probe_proxy_host_reachability(host, device_wifi_ips)"
    return probe


def test_explicit_proxy_host_overrides_candidate_order() -> None:
    """验证显式选择的本机地址优先，避免网卡枚举顺序把手机代理指向错误网络。"""
    select_proxy_host = _load_selector()

    result = select_proxy_host(
        ["10.20.0.8", "192.168.50.10"],
        explicit_host="192.168.50.10",
        device_wifi_ips={"android": "192.168.50.21"},
    )

    assert result["ok"] is True
    assert result["host"] == "192.168.50.10"


def test_proxy_host_is_selected_from_the_subnet_shared_by_every_device() -> None:
    """验证三端 Wi-Fi 地址共同证明候选 Host 可达，而不是无条件接受 candidates[0]。"""
    select_proxy_host = _load_selector()

    result = select_proxy_host(
        ["10.20.0.8", "192.168.50.10"],
        device_wifi_ips={
            "android": "192.168.50.21",
            "ios": "192.168.50.22",
            "harmony": "192.168.50.23",
        },
    )

    assert result["ok"] is True
    assert result["host"] == "192.168.50.10"


def test_proxy_host_selection_preserves_an_advertised_narrow_prefix() -> None:
    """验证设备上报 /25 时不会擅自扩成 /24 并接受实际子网外的 Host。"""
    result = _load_selector()(
        ["192.168.50.10"],
        device_wifi_ips={"android": "192.168.50.130/25"},
    )

    assert result["ok"] is False


@pytest.mark.parametrize(
    "device_wifi_ips",
    [
        {},
        {"android": "172.16.7.21", "ios": "172.16.7.22"},
    ],
)
def test_proxy_host_selection_blocks_when_reachability_cannot_be_proven(
    device_wifi_ips: dict[str, str],
) -> None:
    """验证缺少设备 Wi-Fi 地址或没有共同子网时硬阻断，防止生成看似可用的代理配置。"""
    select_proxy_host = _load_selector()

    result = select_proxy_host(
        ["10.20.0.8", "192.168.50.10"],
        device_wifi_ips=device_wifi_ips,
    )

    assert result["ok"] is False
    assert result["host"] == ""
    assert result["code"] == "proxy_host_unproven"


def test_proxy_host_route_probe_requires_every_device_to_reply() -> None:
    """验证真实路由探测必须覆盖全部设备，任一端失败都会硬阻断。"""
    probe = _load_reachability_probe()
    commands: list[list[str]] = []

    class _Result:
        """Provide the subprocess fields consumed by the reachability probe."""

        def __init__(self, returncode: int) -> None:
            """Initialize one deterministic command result."""
            self.returncode = returncode
            self.stdout = ""
            self.stderr = "timeout" if returncode else ""

    def runner(command: list[str]) -> _Result:
        """Record each route probe and fail only the iOS address."""
        commands.append(command)
        return _Result(1 if command[-1] == "192.168.50.22" else 0)

    result = probe(
        "192.168.50.10",
        {"android": "192.168.50.21", "ios": "192.168.50.22"},
        command_runner=runner,
    )

    assert result["ok"] is False
    assert result["code"] == "proxy_host_unreachable"
    assert len(commands) == 2
