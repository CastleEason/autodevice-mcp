"""Regression tests for durable retained-proxy recovery and workspace exclusion."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mobile_auto_mcp.execution.runner import _retain_managed_environment, run_cases
from mobile_auto_mcp.proxy.device_proxy import DeviceProxyAdapter, ManagedProxyLease, ProxySnapshot
from mobile_auto_mcp.proxy.proxy_manager import ProxyManager
from mobile_auto_mcp.proxy import proxy_manager as proxy_manager_module
from mobile_auto_mcp.state.storage import LocalStore, workspace_home


def _load_recovery_contract() -> tuple[type[Any], type[Any], type[BaseException]]:
    """Load recovery and run-lock seams while reporting missing Red contracts as readable failures."""
    try:
        module = importlib.import_module("mobile_auto_mcp.proxy.recovery")
    except ModuleNotFoundError:
        pytest.fail("缺少 mobile_auto_mcp.proxy.recovery 跨进程恢复模块")
    manager = getattr(module, "ProxyRecoveryManager", None)
    run_lock = getattr(module, "WorkspaceRunLock", None)
    busy_error = getattr(module, "WorkspaceBusyError", None)
    assert manager is not None, "缺少 ProxyRecoveryManager"
    assert run_lock is not None, "缺少 WorkspaceRunLock"
    assert isinstance(busy_error, type) and issubclass(busy_error, BaseException), "缺少 WorkspaceBusyError"
    return manager, run_lock, busy_error


class _RecordingAdapter:
    """Record restoration calls so tests verify persisted snapshots drive a fresh-process adapter."""

    def __init__(self, *, restore_ok: bool = True) -> None:
        """Configure whether restored state verification succeeds for one isolated test device."""
        self.restore_ok = restore_ok
        self.restored: list[ProxySnapshot] = []

    def restore(self, snapshot: ProxySnapshot) -> dict[str, Any]:
        """Capture the persisted snapshot that recovery asks the platform adapter to restore."""
        self.restored.append(snapshot)
        return {"ok": True}

    def verify_restored(self, snapshot: ProxySnapshot) -> dict[str, Any]:
        """Return the test-controlled readback result that gates recovery-record deletion."""
        return {"ok": self.restore_ok, "snapshot": snapshot.target}


def _snapshot() -> ProxySnapshot:
    """Build a non-default proxy snapshot so persistence cannot pass by recreating empty defaults."""
    return ProxySnapshot(
        target="android",
        device_serial="device-a",
        ssid="qa-wifi",
        mode="manual",
        host="192.168.50.99",
        port=8080,
        exclusion_list="localhost",
    )


def _runtime() -> dict[str, Any]:
    """Build owned runtime evidence that must be handed to the injected proxy stopper."""
    return {
        "pid": 4242,
        "port": 13000,
        "home": "/isolated/test/home",
        "addon": "/isolated/test/proxy_addon.py",
    }


class _LeaseAdapter(DeviceProxyAdapter):
    """Record lease operations so durability ordering can be asserted without a phone."""

    def __init__(self, operations: list[str]) -> None:
        """Bind the adapter to a shared ordered operation log."""
        super().__init__("android", "device-a")
        self.operations = operations

    def snapshot(self) -> ProxySnapshot:
        """Return the original state and record that no device write happened yet."""
        self.operations.append("snapshot")
        return _snapshot()

    def apply(self, host: str, port: int) -> dict[str, Any]:
        """Record the first device mutation boundary."""
        self.operations.append("apply")
        return {"ok": True}

    def verify(self, host: str, port: int) -> dict[str, Any]:
        """Acknowledge the managed proxy readback."""
        self.operations.append("verify")
        return {"ok": True}

    def restore(self, snapshot: ProxySnapshot) -> dict[str, Any]:
        """Record rollback when acquisition fails."""
        self.operations.append("restore")
        return {"ok": True}

    def verify_restored(self, snapshot: ProxySnapshot) -> dict[str, Any]:
        """Acknowledge rollback readback."""
        return {"ok": True}


def test_lease_persists_snapshot_before_first_device_write() -> None:
    """验证原代理快照在 apply 前 durable 落盘，关闭崩溃后无恢复依据的窗口。"""
    operations: list[str] = []
    lease = ManagedProxyLease(
        [_LeaseAdapter(operations)],
        "192.168.50.10",
        13000,
        snapshot_sink=lambda snapshots: operations.append("persist"),
    )

    result = lease.acquire()

    assert result["ok"] is True
    assert operations.index("persist") < operations.index("apply")


def test_lease_never_writes_device_when_snapshot_persistence_fails() -> None:
    """验证 durable 保存失败时在任何代理写入前结束 acquisition。"""
    operations: list[str] = []

    def fail_persist(snapshots: dict[str, ProxySnapshot]) -> None:
        """Simulate an unavailable private recovery file."""
        operations.append("persist")
        raise OSError("disk unavailable")

    lease = ManagedProxyLease(
        [_LeaseAdapter(operations)],
        "192.168.50.10",
        13000,
        snapshot_sink=fail_persist,
    )

    result = lease.acquire()

    assert result["ok"] is False
    assert operations == ["snapshot", "persist"]


def test_successful_retention_persists_snapshots_for_a_new_process(tmp_path: Path) -> None:
    """验证正式执行保留代理时立即落盘原配置，进程退出后仍有恢复依据。"""
    recovery_manager_type, _, _ = _load_recovery_contract()
    store = LocalStore(tmp_path / "workspace")
    session = store.start_session("android", [])
    snapshot = _snapshot()
    proxy_manager = ProxyManager(store.home, target="android", port=13000)
    proxy_manager.runtime_path.parent.mkdir(parents=True, exist_ok=True)
    proxy_manager.runtime_path.write_text(json.dumps(_runtime()), encoding="utf-8")
    # acquire 结果和 lease 同时携带快照，覆盖真实 Runner 的内存对象与可序列化证据两条来源。
    readiness = {
        "lease": SimpleNamespace(snapshots={"android": snapshot}),
        "manager": proxy_manager,
        "proxy_host": "192.168.50.10",
        "proxy_port": 13000,
        "proxy_lifecycle": {"ok": True, "state": "active", "snapshots": {"android": snapshot.__dict__}},
    }

    lifecycle = _retain_managed_environment(readiness, store, session["session_id"])
    pending = recovery_manager_type(store.home).load_pending()

    assert lifecycle["status"] == "retained"
    assert pending is not None
    assert pending["session_id"] == session["session_id"]
    assert pending["snapshots"]["android"]["host"] == "192.168.50.99"
    assert pending["proxy_runtime"]["pid"] == 4242


def test_new_process_restores_every_device_then_stops_owned_proxy_and_clears_record(tmp_path: Path) -> None:
    """验证跨进程恢复仅在设备读回成功且 owned mitmproxy 停止成功后删除恢复记录。"""
    recovery_manager_type, _, _ = _load_recovery_contract()
    first_process = recovery_manager_type(tmp_path)
    first_process.persist("session-a", {"android": _snapshot().__dict__}, _runtime())
    adapter = _RecordingAdapter()
    stopped: list[dict[str, Any]] = []

    # 新实例模拟原 Runner 已退出；恢复不能依赖旧 lease 或 Popen 内存对象。
    second_process = recovery_manager_type(tmp_path)
    result = second_process.restore_and_stop(
        adapter_factory=lambda target, device_serial: adapter,
        proxy_stopper=lambda runtime: stopped.append(runtime) or {"ok": True},
    )

    assert result["ok"] is True
    assert adapter.restored == [_snapshot()]
    assert stopped == [_runtime()]
    assert second_process.load_pending() is None


def test_later_retained_run_never_overwrites_the_original_device_snapshot(tmp_path: Path) -> None:
    """验证连续执行复用代理时保留最早原配置，避免把 MCP 手动代理误当成用户原始状态。"""
    recovery_manager_type, _, _ = _load_recovery_contract()
    manager = recovery_manager_type(tmp_path)
    original = _snapshot()
    already_managed = {**original.__dict__, "host": "192.168.50.10", "port": 13000}

    manager.persist("session-a", {"android": original.__dict__}, _runtime())
    manager.persist("session-b", {"android": already_managed}, _runtime())
    pending = manager.load_pending()

    assert pending is not None
    assert pending["session_id"] == "session-b"
    assert pending["session_ids"] == ["session-a", "session-b"]
    assert pending["snapshots"]["android"]["host"] == "192.168.50.99"


@pytest.mark.parametrize("restore_ok,stop_ok", [(False, True), (True, False)])
def test_recovery_record_survives_device_or_proxy_cleanup_failure(
    tmp_path: Path,
    restore_ok: bool,
    stop_ok: bool,
) -> None:
    """验证任何恢复或停止失败都保留完整记录，使下一进程可以继续重试而不丢失现场。"""
    recovery_manager_type, _, _ = _load_recovery_contract()
    manager = recovery_manager_type(tmp_path)
    manager.persist("session-a", {"android": _snapshot().__dict__}, _runtime())
    adapter = _RecordingAdapter(restore_ok=restore_ok)

    result = manager.restore_and_stop(
        adapter_factory=lambda target, device_serial: adapter,
        proxy_stopper=lambda runtime: {"ok": stop_ok},
    )

    assert result["ok"] is False
    assert manager.load_pending() is not None


def test_workspace_run_lock_rejects_a_second_owner(tmp_path: Path) -> None:
    """验证同一 workspace 同时只允许一个 Runner 改写共享代理状态，避免 active/probe 文件互相覆盖。"""
    _, run_lock_type, busy_error_type = _load_recovery_contract()
    first = run_lock_type(tmp_path, owner="session-a")
    second = run_lock_type(tmp_path, owner="session-b")

    first.acquire()
    try:
        with pytest.raises(busy_error_type):
            second.acquire()
    finally:
        first.release()

    # 首个 owner 释放后必须允许后续任务接管，避免失败任务永久锁死 workspace。
    second.acquire()
    second.release()


def test_store_mutations_do_not_lose_updates_across_processes(tmp_path: Path) -> None:
    """验证两个 MCP 进程并发写 Session 时使用同一文件锁，最终记录数完整。"""
    script = (
        "from mobile_auto_mcp.state.storage import LocalStore; "
        f"store=LocalStore({str(tmp_path)!r}); "
        "[store.start_session('android',[str(i)]) for i in range(20)]"
    )
    processes = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(2)]

    assert [process.wait(timeout=10) for process in processes] == [0, 0]
    assert len(LocalStore(tmp_path).list_sessions()) == 40


def test_cleanup_accepts_an_owned_runtime_whose_process_already_exited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 owned PID 已自然退出时清理可幂等成功，而不是把恢复记录永久卡在 ownership_mismatch。"""
    manager = ProxyManager(tmp_path, target="android", port=13000)
    runtime = {
        "pid": 4242,
        "port": 13000,
        "home": str(tmp_path.resolve()),
        "addon": str(manager.addon_path.resolve()),
    }
    manager.runtime_path.parent.mkdir(parents=True, exist_ok=True)
    manager.runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
    monkeypatch.setattr(proxy_manager_module, "_pid_exists", lambda pid: False)

    result = manager.stop_owned_retained(runtime)

    assert result["ok"] is True
    assert result["status"] == "already_stopped"
    assert not manager.runtime_path.exists()


def test_run_cases_rejects_an_already_locked_workspace_before_preflight(tmp_path: Path) -> None:
    """验证 Runner 真正接入工作区锁，并在设备检查或共享状态写入前返回稳定的忙碌结果。"""
    _, run_lock_type, _ = _load_recovery_contract()
    home = workspace_home(base_home=tmp_path, app_id="demo-app")
    blocker = run_lock_type(home, owner="existing-run")

    blocker.acquire()
    try:
        result = run_cases(
            app_id="demo-app",
            base_home=str(tmp_path),
            target="android",
            proxy_required=False,
        )
    finally:
        blocker.release()

    assert result["ok"] is False
    assert result["status"] == "workspace_busy"
