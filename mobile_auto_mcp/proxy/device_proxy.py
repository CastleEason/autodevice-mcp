"""Safe device HTTP proxy snapshots and managed lifecycle leases."""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ProxySnapshot:
    """Describe one device's original Wi-Fi HTTP proxy configuration."""

    target: str
    device_serial: str
    ssid: str = ""
    mode: str = "none"
    host: str = ""
    port: int = 0
    auto_config_url: str = ""
    exclusion_list: str = ""
    raw: dict[str, Any] | None = None


class DeviceProxyAdapter:
    """Define the platform boundary required by a managed proxy lease."""

    def __init__(self, target: str, device_serial: str = "") -> None:
        """Bind the adapter to one platform device without storing a network address."""
        self.target = target.lower()
        self.device_serial = device_serial

    def snapshot(self) -> ProxySnapshot:
        """Read the complete original proxy state before any mutation."""
        raise NotImplementedError

    def apply(self, host: str, port: int) -> dict[str, Any]:
        """Apply the lease proxy to the bound device."""
        raise NotImplementedError

    def verify(self, host: str, port: int) -> dict[str, Any]:
        """Read back and verify the lease proxy after applying it."""
        raise NotImplementedError

    def restore(self, snapshot: ProxySnapshot) -> dict[str, Any]:
        """Restore the exact proxy mode and values captured in the snapshot."""
        raise NotImplementedError

    def verify_restored(self, snapshot: ProxySnapshot) -> dict[str, Any]:
        """Read back and verify the original proxy state after restoration."""
        raise NotImplementedError


class AndroidProxyAdapter(DeviceProxyAdapter):
    """Manage Android global HTTP proxy settings through ADB with exact restoration."""

    _KEYS = (
        "http_proxy",
        "global_http_proxy_host",
        "global_http_proxy_port",
        "global_http_proxy_exclusion_list",
        "global_proxy_pac_url",
    )

    def __init__(
        self,
        device_serial: str = "",
        shell: Callable[[list[str], str], str] | None = None,
    ) -> None:
        """Use an injectable shell boundary so lifecycle behavior is deterministic in tests."""
        super().__init__("android", device_serial)
        self._shell = shell or _android_adb_shell

    def snapshot(self) -> ProxySnapshot:
        """Read Android proxy keys and current SSID into an immutable snapshot."""
        values = {key: self._get(key) for key in self._KEYS}
        host, port = _proxy_host_port(values)
        auto_config_url = _clean_setting(values.get("global_proxy_pac_url"))
        mode = "auto" if auto_config_url else ("manual" if host and port else "none")
        ssid_output = self._shell(["dumpsys", "wifi"], self.device_serial)
        return ProxySnapshot(
            target=self.target,
            device_serial=self.device_serial,
            ssid=_android_ssid(ssid_output),
            mode=mode,
            host=host,
            port=port,
            auto_config_url=auto_config_url,
            exclusion_list=_clean_setting(values.get("global_http_proxy_exclusion_list")),
            raw=values,
        )

    def apply(self, host: str, port: int) -> dict[str, Any]:
        """Write all Android proxy keys to the lease host and port."""
        if not host or int(port) <= 0:
            raise ValueError("Android 代理 host 和 port 必须有效")
        self._put("http_proxy", f"{host}:{int(port)}")
        self._put("global_http_proxy_host", host)
        self._put("global_http_proxy_port", str(int(port)))
        self._put("global_http_proxy_exclusion_list", "")
        self._delete("global_proxy_pac_url")
        return {"ok": True, "target": self.target, "host": host, "port": int(port)}

    def verify(self, host: str, port: int) -> dict[str, Any]:
        """Verify Android's effective proxy matches the managed lease."""
        current = self.snapshot()
        ok = current.mode == "manual" and current.host == host and current.port == int(port)
        return {"ok": ok, "expected": {"host": host, "port": int(port)}, "actual": asdict(current)}

    def restore(self, snapshot: ProxySnapshot) -> dict[str, Any]:
        """Restore every captured Android proxy key, including an originally disabled proxy."""
        raw = snapshot.raw or {}
        for key in self._KEYS:
            value = _clean_setting(raw.get(key))
            if value:
                self._put(key, value)
            else:
                self._delete(key)
        if snapshot.mode == "none":
            # Android uses :0 as the explicit disabled value; write it last to prevent stale host/port reuse.
            self._put("http_proxy", ":0")
        return {"ok": True, "target": self.target, "restored": asdict(snapshot)}

    def verify_restored(self, snapshot: ProxySnapshot) -> dict[str, Any]:
        """Compare the normalized effective Android proxy with its original snapshot."""
        current = self.snapshot()
        ok = (
            current.mode == snapshot.mode
            and current.host == snapshot.host
            and current.port == snapshot.port
            and current.auto_config_url == snapshot.auto_config_url
            and current.exclusion_list == snapshot.exclusion_list
        )
        return {"ok": ok, "expected": asdict(snapshot), "actual": asdict(current)}

    def _get(self, key: str) -> str:
        """Read one Android global setting without mutating device state."""
        return str(self._shell(["settings", "get", "global", key], self.device_serial) or "").strip()

    def _put(self, key: str, value: str) -> None:
        """Write one Android global setting through the injected ADB boundary."""
        self._shell(["settings", "put", "global", key, value], self.device_serial)

    def _delete(self, key: str) -> None:
        """Delete one Android global setting when it was absent in the snapshot."""
        self._shell(["settings", "delete", "global", key], self.device_serial)


class SemanticSettingsProxyAdapter(DeviceProxyAdapter):
    """Manage iOS or HarmonyOS proxy state through a semantic device-driver contract."""

    def __init__(self, target: str, device_serial: str, driver: Any) -> None:
        """Bind a driver that can read and configure the current Wi-Fi proxy semantically."""
        super().__init__(target, device_serial)
        if driver is None:
            raise ValueError(f"{target} 代理托管需要已连接的 DeviceDriver")
        self.driver = driver

    def snapshot(self) -> ProxySnapshot:
        """Read the current Wi-Fi proxy through the driver's semantic settings workflow."""
        payload = self._read()
        if not payload.get("ok", False):
            raise RuntimeError(str(payload.get("message") or payload.get("failure") or "系统代理读取失败"))
        ssid = str(payload.get("ssid") or "").strip()
        if not ssid:
            raise RuntimeError("系统设置未识别当前 Wi-Fi SSID，禁止创建不可验证的代理快照")
        return ProxySnapshot(
            target=self.target,
            device_serial=self.device_serial,
            ssid=ssid,
            mode=str(payload.get("mode") or "none"),
            host=str(payload.get("host") or ""),
            port=int(payload.get("port") or 0),
            auto_config_url=str(payload.get("auto_config_url") or ""),
            exclusion_list=str(payload.get("exclusion_list") or ""),
            raw=dict(payload),
        )

    def apply(self, host: str, port: int) -> dict[str, Any]:
        """Set a manual proxy through semantic settings locators, never business coordinates."""
        return self._configure(mode="manual", host=host, port=int(port), auto_config_url="")

    def verify(self, host: str, port: int) -> dict[str, Any]:
        """Verify the semantic settings readback matches the managed proxy."""
        current = self.snapshot()
        ok = current.mode == "manual" and current.host == host and current.port == int(port)
        return {"ok": ok, "expected": {"host": host, "port": int(port)}, "actual": asdict(current)}

    def restore(self, snapshot: ProxySnapshot) -> dict[str, Any]:
        """Restore off, manual, or automatic mode using only the captured snapshot values."""
        current = self.snapshot()
        if snapshot.ssid and current.ssid != snapshot.ssid:
            # Wi-Fi proxy settings are network-scoped; restoring on another SSID can corrupt unrelated connectivity.
            raise RuntimeError(
                f"当前 SSID {current.ssid!r} 与代理快照 SSID {snapshot.ssid!r} 不一致，拒绝恢复"
            )
        return self._configure(
            mode=snapshot.mode,
            host=snapshot.host,
            port=snapshot.port,
            auto_config_url=snapshot.auto_config_url,
        )

    def verify_restored(self, snapshot: ProxySnapshot) -> dict[str, Any]:
        """Compare semantic readback with the original proxy mode and values."""
        current = self.snapshot()
        ok = (
            (not snapshot.ssid or current.ssid == snapshot.ssid)
            and
            current.mode == snapshot.mode
            and current.host == snapshot.host
            and current.port == snapshot.port
            and current.auto_config_url == snapshot.auto_config_url
        )
        return {"ok": ok, "expected": asdict(snapshot), "actual": asdict(current)}

    def _read(self) -> dict[str, Any]:
        """Call the explicit system-proxy read capability and reject unsupported drivers safely."""
        method = getattr(self.driver, "read_system_proxy", None)
        if not callable(method):
            raise RuntimeError(f"{self.target} DeviceDriver 缺少 read_system_proxy，禁止盲改代理")
        return dict(method())

    def _configure(self, **configuration: Any) -> dict[str, Any]:
        """Call the explicit system-proxy mutation capability and require an acknowledged result."""
        method = getattr(self.driver, "configure_system_proxy", None)
        if not callable(method):
            raise RuntimeError(f"{self.target} DeviceDriver 缺少 configure_system_proxy，禁止盲改代理")
        result = dict(method(**configuration))
        if not result.get("ok", False):
            raise RuntimeError(str(result.get("message") or result.get("failure") or "系统代理设置失败"))
        return result


class ManagedProxyLease:
    """Own snapshot, apply, verification, rollback, and idempotent restoration for all devices."""

    def __init__(
        self,
        adapters: list[DeviceProxyAdapter],
        host: str,
        port: int,
        event_sink: Callable[[dict[str, Any]], Any] | None = None,
        snapshot_sink: Callable[[dict[str, ProxySnapshot]], Any] | None = None,
    ) -> None:
        """Create a pending lease without touching any device until acquire is called."""
        self.adapters = adapters
        self.host = host
        self.port = int(port)
        self.event_sink = event_sink
        self.snapshot_sink = snapshot_sink
        self.snapshots: dict[str, ProxySnapshot] = {}
        self._modified: list[DeviceProxyAdapter] = []
        self.state = "pending"
        self.events: list[dict[str, Any]] = []
        self.last_release: dict[str, Any] | None = None

    def acquire(self) -> dict[str, Any]:
        """Snapshot every device first, then apply and verify each proxy with rollback on failure."""
        if self.state == "active":
            return {"ok": True, "state": self.state, "already_acquired": True}
        self._emit("lease_acquire_started")
        try:
            for adapter in self.adapters:
                snapshot = adapter.snapshot()
                self.snapshots[adapter.target] = snapshot
                self._emit("proxy_snapshot_saved", target=adapter.target, snapshot=asdict(snapshot))
        except Exception as exc:
            self.state = "acquire_failed"
            failure = _lease_failure("snapshot", adapter, exc)
            self._emit("lease_acquire_failed", **failure)
            return {"ok": False, "state": self.state, "failure": failure, "events": list(self.events)}

        if self.snapshot_sink:
            try:
                # Original settings must be durable before the first apply call can change a phone.
                self.snapshot_sink(dict(self.snapshots))
                self._emit("proxy_snapshots_persisted", targets=sorted(self.snapshots))
            except Exception as exc:
                self.state = "acquire_failed"
                failure = {
                    "code": "proxy_recovery_persist_failed",
                    "stage": "snapshot_persist",
                    "message": str(exc),
                    "error_type": exc.__class__.__name__,
                }
                self._emit("lease_acquire_failed", **failure)
                return {"ok": False, "state": self.state, "failure": failure, "events": list(self.events)}

        try:
            for adapter in self.adapters:
                original = self.snapshots[adapter.target]
                if original.ssid:
                    current = adapter.snapshot()
                    if current.ssid != original.ssid:
                        raise RuntimeError(
                            f"{adapter.target} 当前 SSID {current.ssid!r} 与快照 {original.ssid!r} 不一致"
                        )
                # Mark before apply because a raised write may have partially changed the device.
                self._modified.append(adapter)
                applied = adapter.apply(self.host, self.port)
                verified = adapter.verify(self.host, self.port)
                self._emit("proxy_applied", target=adapter.target, applied=applied, verification=verified)
                if not verified.get("ok", False):
                    raise RuntimeError("代理设置后的读取复核不一致")
        except Exception as exc:
            failure = _lease_failure("apply", adapter, exc)
            rollback = self._restore_modified(reason="acquire_rollback")
            self.state = "acquire_failed"
            self._emit("lease_acquire_failed", **failure, rollback=rollback)
            return {
                "ok": False,
                "state": self.state,
                "failure": failure,
                "rollback": rollback,
                "events": list(self.events),
            }

        self.state = "active"
        self._emit("lease_acquired", host=self.host, port=self.port)
        return {
            "ok": True,
            "state": self.state,
            "host": self.host,
            "port": self.port,
            "snapshots": {target: asdict(snapshot) for target, snapshot in self.snapshots.items()},
            "events": list(self.events),
        }

    def release(self) -> dict[str, Any]:
        """Restore and verify every modified device; repeat safely only after an earlier cleanup failure."""
        if self.state == "released" and self.last_release:
            return {**self.last_release, "already_released": True}
        if not self._modified:
            self.state = "released"
            self.last_release = {"ok": True, "verified": True, "state": self.state, "devices": []}
            return dict(self.last_release)
        self._emit("lease_release_started")
        result = self._restore_modified(reason="release")
        self.state = "released" if result["ok"] else "cleanup_failed"
        self.last_release = {**result, "state": self.state}
        self._emit("lease_released" if result["ok"] else "lease_release_failed", result=result)
        return dict(self.last_release)

    def _restore_modified(self, reason: str) -> dict[str, Any]:
        """Restore modified adapters in reverse order and preserve every failure for repair guidance."""
        devices: list[dict[str, Any]] = []
        all_ok = True
        for adapter in reversed(self._modified):
            snapshot = self.snapshots[adapter.target]
            try:
                restored = adapter.restore(snapshot)
                verification = adapter.verify_restored(snapshot)
                ok = bool(restored.get("ok", False) and verification.get("ok", False))
                devices.append(
                    {"target": adapter.target, "ok": ok, "restored": restored, "verification": verification}
                )
                all_ok = all_ok and ok
            except Exception as exc:
                all_ok = False
                devices.append({"target": adapter.target, "ok": False, "error": str(exc), "error_type": exc.__class__.__name__})
        if all_ok:
            self._modified.clear()
        return {"ok": all_ok, "verified": all_ok, "reason": reason, "devices": devices}

    def _emit(self, event: str, **payload: Any) -> dict[str, Any]:
        """Append one lifecycle event locally and forward it to the optional persistent sink."""
        row = {"event": event, "event_at": time.time(), **payload}
        self.events.append(row)
        if self.event_sink:
            self.event_sink(row)
        return row


def build_device_proxy_adapter(target: str, device_serial: str, driver: Any = None) -> DeviceProxyAdapter:
    """Build the safe platform adapter without accepting any device/client IP mapping."""
    normalized = target.lower()
    if normalized == "android":
        return AndroidProxyAdapter(device_serial)
    if normalized in {"ios", "harmony"}:
        return SemanticSettingsProxyAdapter(normalized, device_serial, driver)
    raise ValueError(f"不支持的代理目标端: {target}")


def _lease_failure(stage: str, adapter: DeviceProxyAdapter, exc: Exception) -> dict[str, Any]:
    """Normalize adapter exceptions into a stable lifecycle failure payload."""
    return {
        "stage": stage,
        "code": f"device_proxy_{stage}_failed",
        "target": adapter.target,
        "device_serial": adapter.device_serial,
        "message": str(exc),
        "error_type": exc.__class__.__name__,
    }


def _android_adb_shell(args: list[str], device_serial: str = "") -> str:
    """Execute one bounded ADB shell command for the selected device."""
    command = ["adb"] + (["-s", device_serial] if device_serial else []) + ["shell", *args]
    completed = subprocess.run(command, text=True, capture_output=True, timeout=8)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "ADB command failed").strip())
    return (completed.stdout or "").strip()


def _clean_setting(value: Any) -> str:
    """Normalize Android's null-like settings output into an absent value."""
    text = str(value or "").strip()
    return "" if text.lower() in {"null", "none", "undefined"} else text


def _proxy_host_port(values: dict[str, Any]) -> tuple[str, int]:
    """Resolve Android proxy host and port from combined or split global settings."""
    combined = _clean_setting(values.get("http_proxy"))
    host = _clean_setting(values.get("global_http_proxy_host"))
    port_text = _clean_setting(values.get("global_http_proxy_port"))
    if combined and combined not in {":0", "0"} and ":" in combined:
        combined_host, combined_port = combined.rsplit(":", 1)
        host = combined_host or host
        port_text = combined_port or port_text
    try:
        port = int(port_text or 0)
    except ValueError:
        port = 0
    return (host if port > 0 else "", port if host and port > 0 else 0)


def _android_ssid(dumpsys_wifi: str) -> str:
    """Extract the current Android SSID when dumpsys exposes it, without requiring it for safety."""
    match = re.search(r'(?:SSID|ssid)[:=]\s*"?([^,"\n]+)', str(dumpsys_wifi or ""))
    return match.group(1).strip() if match else ""
