"""Regression tests for the iOS WDA trust and execution readiness gate."""

from __future__ import annotations

from typing import Any

import pytest

from mobile_auto_mcp.execution import preflight, runner
from mobile_auto_mcp.execution.adapters.ios import IOSWDAClient, WDAConnectionError
from mobile_auto_mcp.execution.preflight import PreflightResult


def _successful_preflight(target: str) -> PreflightResult:
    """Build a minimal successful preflight for runner gate tests."""
    return PreflightResult(
        ok=True,
        target=target,
        proxy_required=False,
        expected_proxy_port=13000,
        checks={},
        blockers=[],
        warnings=[],
        failures=[],
        phone_proxy_hint="",
        proxy_instruction={},
    )


def test_wda_readiness_rejects_status_that_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 WDA HTTP 可访问但 ready=false 时仍视为信任/Runner 未就绪。"""
    client = IOSWDAClient("http://127.0.0.1:8100")
    monkeypatch.setattr(client, "status", lambda: {"value": {"ready": False, "message": "not ready"}})

    with pytest.raises(WDAConnectionError, match="ready=false"):
        client.verify_readiness()


def test_wda_readiness_requires_session_and_read_only_window_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 /status 成功后仍必须创建 session 并完成基础只读动作。"""
    client = IOSWDAClient("http://127.0.0.1:8100")
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return a ready status but fail at session creation."""
        calls.append((method, path))
        if path == "/status":
            return {"value": {"ready": True}, "sessionId": None}
        if path == "/session":
            raise WDAConnectionError("developer trust is not accepted")
        raise AssertionError(f"unexpected WDA request: {method} {path} {payload}")

    monkeypatch.setattr(client, "_request", fake_request)

    with pytest.raises(WDAConnectionError, match="session"):
        client.verify_readiness()

    assert calls == [("GET", "/status"), ("POST", "/session")]


def test_wda_readiness_accepts_only_positive_window_dimensions_and_cleans_probe_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 WDA 门禁完成 status、session、窗口尺寸三段检查并清理临时 session。"""
    client = IOSWDAClient("http://127.0.0.1:8100")
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Provide a complete successful WDA readiness exchange."""
        calls.append((method, path))
        if path == "/status":
            return {"value": {"ready": True, "message": "ready"}, "sessionId": None}
        if path == "/session":
            return {"value": {"sessionId": "probe-session"}, "sessionId": "probe-session"}
        if path == "/session/probe-session/window/size":
            return {"value": {"width": 390, "height": 844}}
        if method == "DELETE" and path == "/session/probe-session":
            return {"value": None}
        raise AssertionError(f"unexpected WDA request: {method} {path} {payload}")

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.verify_readiness()

    assert result["ok"] is True
    assert result["checks"]["status_ready"] is True
    assert result["checks"]["session"]["session_id"] == "probe-session"
    assert result["checks"]["window_size"] == {"width": 390, "height": 844}
    assert result["checks"]["temporary_session_deleted"] is True
    assert calls == [
        ("GET", "/status"),
        ("POST", "/session"),
        ("GET", "/session/probe-session/window/size"),
        ("DELETE", "/session/probe-session"),
    ]


def test_ios_preflight_blocks_when_full_wda_readiness_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 iOS 前置检查不会把仅能访问 /status 的 WDA 误判为可执行。"""
    monkeypatch.setattr(preflight, "host_capability", lambda target: {"ok": True, "host_platform": "darwin"})
    monkeypatch.setattr(preflight, "_port_free", lambda port: True)
    monkeypatch.setattr(
        preflight,
        "_wda_status",
        lambda url: {
            "ok": False,
            "stage": "session",
            "error": "WDA status 可访问，但 session 创建失败；请在真机确认开发者信任",
        },
    )

    result = preflight.run_preflight(target="ios", proxy_required=False)

    assert result.ok is False
    assert result.checks["wda"]["stage"] == "session"
    assert any("session 创建失败" in blocker for blocker in result.blockers)


def test_failed_ios_wda_preflight_stops_before_driver_creation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 WDA 强门禁失败后 Runner 不会进入设备连接、导航或代理设置阶段。"""
    ios_failure = _successful_preflight("ios")
    ios_failure.ok = False
    ios_failure.blockers.append("WDA session 未就绪")

    monkeypatch.setattr(runner, "_run_preflight_with_budget", lambda *args, **kwargs: ios_failure)

    def unexpected_driver(*args: Any, **kwargs: Any) -> Any:
        """Fail if runner creates a driver after the iOS preflight was rejected."""
        raise AssertionError(f"driver must not be created: {args!r} {kwargs!r}")

    monkeypatch.setattr(runner, "_make_driver", unexpected_driver)

    result = runner.prepare_managed_environment(
        store=runner.LocalStore(tmp_path),
        targets=["ios"],
        device_serials={"ios": "ios-device"},
        proxy_required=False,
        proxy_port=13000,
        proxy_host="",
        wda_url="http://127.0.0.1:8100",
        auto_start_wda=False,
        allow_wda_reinstall=False,
        wda_start_command="",
        wda_iproxy_command="",
    )

    assert result["ok"] is False
    assert result["status"] == "readiness_failed"
