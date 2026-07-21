"""Durable retained-proxy recovery and cross-process workspace exclusion."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from mobile_auto_mcp.platform.file_lock import lock_file, unlock_file
from mobile_auto_mcp.proxy.device_proxy import ProxySnapshot
from mobile_auto_mcp.state.private_files import atomic_write_private_text, ensure_private_directory


class WorkspaceBusyError(RuntimeError):
    """Report that another process currently owns the workspace's mutable proxy state."""


class WorkspaceRunLock:
    """Hold an advisory process lock for one workspace while shared proxy files are mutable."""

    def __init__(self, home: str | Path, owner: str) -> None:
        """Bind the lock to a workspace and an audit-friendly run owner identifier."""
        self.home = Path(home).expanduser()
        self.owner = str(owner or "unknown")
        self.path = self.home / "proxy" / "workspace_run.lock"
        self._descriptor: int | None = None

    def acquire(self) -> "WorkspaceRunLock":
        """Acquire the workspace lock without waiting, raising a stable busy error on contention."""
        if self._descriptor is not None:
            return self
        ensure_private_directory(self.path.parent)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        if hasattr(os, "fchmod"):
            # POSIX honors descriptor permissions; Windows relies on the private proxy directory.
            os.fchmod(descriptor, 0o600)
        try:
            # Non-blocking acquisition prevents a second Runner from reaching preflight or shared proxy writes.
            lock_file(descriptor, blocking=False)
        except BlockingIOError as exc:
            os.close(descriptor)
            current_owner = self._read_owner()
            raise WorkspaceBusyError(
                f"工作区正在被 {current_owner or '另一个任务'} 使用"
            ) from exc
        metadata = json.dumps(
            {"owner": self.owner, "pid": os.getpid(), "acquired_at": time.time()},
            ensure_ascii=False,
        ).encode("utf-8")
        os.ftruncate(descriptor, 0)
        os.write(descriptor, metadata)
        os.fsync(descriptor)
        self._descriptor = descriptor
        return self

    def release(self) -> None:
        """Release the held lock while leaving its inode in place to avoid an unlink/acquire race."""
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            unlock_file(descriptor)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "WorkspaceRunLock":
        """Acquire this lock for use as a context-managed Runner boundary."""
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        """Release this lock regardless of whether the protected Runner branch failed."""
        self.release()

    def _read_owner(self) -> str:
        """Read the current lock owner's identifier for a useful contention response."""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            return ""
        return str(payload.get("owner") or "") if isinstance(payload, dict) else ""


class ProxyRecoveryManager:
    """Persist original device proxy snapshots and restore them from a later process."""

    def __init__(self, home: str | Path) -> None:
        """Bind recovery state to one isolated workspace without retaining process-local objects."""
        self.home = Path(home).expanduser()
        self.path = self.home / "proxy" / "pending_proxy_recovery.json"

    def persist(
        self,
        session_id: str,
        snapshots: Mapping[str, Mapping[str, Any]],
        proxy_runtime: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Atomically save device snapshots and owned proxy evidence for crash-safe later cleanup."""
        existing = self.load_pending()
        if existing and existing.get("invalid"):
            raise RuntimeError("已有代理恢复记录损坏，禁止覆盖原始设备状态")
        previous_runtime = dict((existing or {}).get("proxy_runtime") or {})
        current_runtime = dict(proxy_runtime)
        if previous_runtime and not _same_owned_runtime(previous_runtime, current_runtime):
            raise RuntimeError("已有代理恢复记录属于不同的 managed mitmproxy，禁止覆盖")
        previous_snapshots = dict((existing or {}).get("snapshots") or {})
        # The earliest snapshot is the only evidence of the operator's original state. Later runs may
        # observe the already-managed proxy, so existing targets always win while new targets are added.
        merged_snapshots = {str(target): dict(snapshot) for target, snapshot in snapshots.items()}
        merged_snapshots.update({str(target): dict(snapshot) for target, snapshot in previous_snapshots.items()})
        session_ids = [
            str(value)
            for value in ((existing or {}).get("session_ids") or [(existing or {}).get("session_id")])
            if value
        ]
        if str(session_id) not in session_ids:
            session_ids.append(str(session_id))
        payload = {
            "version": 1,
            "session_id": str(session_id),
            "session_ids": session_ids,
            "created_at": float((existing or {}).get("created_at") or time.time()),
            "updated_at": time.time(),
            "snapshots": merged_snapshots,
            "proxy_runtime": previous_runtime or current_runtime,
        }
        # Recovery evidence can contain device and network identifiers, so it must remain owner-readable only.
        atomic_write_private_text(self.path, json.dumps(payload, ensure_ascii=False, indent=2))
        return payload

    def load_pending(self) -> dict[str, Any] | None:
        """Load one pending recovery record, returning none only when no record exists."""
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"invalid": True, "error": str(exc), "snapshots": {}, "proxy_runtime": {}}
        return payload if isinstance(payload, dict) else {"invalid": True, "snapshots": {}, "proxy_runtime": {}}

    def clear(self) -> None:
        """Delete recovery evidence after every device and the owned proxy have been verified clean."""
        self.path.unlink(missing_ok=True)

    def restore_and_stop(
        self,
        *,
        adapter_factory: Callable[[str, str], Any],
        proxy_stopper: Callable[[dict[str, Any]], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Restore all persisted device snapshots, stop the owned proxy, and clear evidence only on full success."""
        pending = self.load_pending()
        if pending is None:
            return {"ok": True, "status": "nothing_pending", "devices": [], "proxy": {"ok": True}}
        if pending.get("invalid"):
            return {"ok": False, "status": "invalid_recovery_record", "devices": [], "proxy": {}}

        devices: list[dict[str, Any]] = []
        snapshots = pending.get("snapshots") or {}
        if not isinstance(snapshots, dict) or not snapshots:
            return {"ok": False, "status": "invalid_recovery_record", "devices": [], "proxy": {}}
        for target, raw_snapshot in snapshots.items():
            result = self._restore_device(str(target), raw_snapshot, adapter_factory)
            devices.append(result)

        if not all(device.get("ok", False) for device in devices):
            # Preserve both the proxy process and recovery record so a later attempt can retry the full cleanup.
            return {"ok": False, "status": "device_restore_failed", "devices": devices, "proxy": {}}

        runtime = pending.get("proxy_runtime") or {}
        try:
            proxy_result = dict(proxy_stopper(dict(runtime)))
        except Exception as exc:
            proxy_result = {"ok": False, "error": str(exc), "error_type": exc.__class__.__name__}
        if not proxy_result.get("ok", False):
            return {"ok": False, "status": "proxy_stop_failed", "devices": devices, "proxy": proxy_result}

        self.clear()
        return {"ok": True, "status": "recovered", "devices": devices, "proxy": proxy_result}

    def _restore_device(
        self,
        target: str,
        raw_snapshot: Any,
        adapter_factory: Callable[[str, str], Any],
    ) -> dict[str, Any]:
        """Recreate one immutable snapshot, restore it through a fresh adapter, and require readback verification."""
        try:
            if not isinstance(raw_snapshot, dict):
                raise ValueError("设备代理快照不是对象")
            snapshot = ProxySnapshot(**raw_snapshot)
            adapter = adapter_factory(target, snapshot.device_serial)
            restored = adapter.restore(snapshot)
            verification = adapter.verify_restored(snapshot)
            ok = bool(restored.get("ok", False) and verification.get("ok", False))
            return {
                "target": target,
                "ok": ok,
                "restored": restored,
                "verification": verification,
            }
        except Exception as exc:
            return {"target": target, "ok": False, "error": str(exc), "error_type": exc.__class__.__name__}


def _same_owned_runtime(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Compare durable process identity fields before allowing a recovery record to be extended."""
    fields = ("pid", "port", "home", "addon")
    return bool(right) and all(str(left.get(field) or "") == str(right.get(field) or "") for field in fields)
