"""File-based state shared by runner and mitmproxy addon."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from mobile_auto_mcp.state.private_files import append_private_text, atomic_write_private_text, ensure_private_directory


class ProxyState:
    """Synchronize active rule, probe mode, recent requests, and hit evidence."""

    def __init__(self, home: str | Path) -> None:
        """初始化同一工作区的原子代理状态、命中证据和改写响应证据路径。"""
        self.home = Path(home)
        self.proxy_dir = self.home / "proxy"
        self.hits_dir = self.proxy_dir / "hits"
        ensure_private_directory(self.proxy_dir)
        ensure_private_directory(self.hits_dir)
        self.active_path = self.proxy_dir / "active_rule.json"
        self.probe_path = self.proxy_dir / "probe_rule.json"
        self.recent_requests_path = self.proxy_dir / "recent_requests.json"
        self.events_path = self.proxy_dir / "mitmproxy_events.jsonl"
        self.modified_responses_path = self.proxy_dir / "modified_responses.jsonl"
        self.lock_path = self.proxy_dir / "state.lock"
        self._active_lock = threading.RLock()

    def set_active(self, session_id: str, target: str, rule: dict[str, Any]) -> None:
        """Persist active using the supplied state and inputs."""
        self.set_active_lanes(session_id, {target or "default": {"target": target, "rule": rule, "client_ip": ""}})

    def set_active_lanes(self, session_id: str, lanes: dict[str, dict[str, Any]]) -> str:
        """Persist active lanes using the supplied state and inputs."""
        activation_id = uuid4().hex
        normalized = _normalize_lanes(lanes)
        for lane in normalized.values():
            lane["activation_id"] = str(lane.get("activation_id") or activation_id)
        with self._active_lock, self._file_lock():
            self._write_json(
                self.active_path,
                {
                    "session_id": session_id,
                    "activation_id": activation_id,
                    "mode": "lanes",
                    "lanes": normalized,
                    "updated_at": time.time(),
                },
            )
        return activation_id

    def set_active_lane(self, session_id: str, lane_id: str, lane: dict[str, Any]) -> str:
        """Replace one lane rule without invalidating requests owned by other lanes."""
        activation_id = str(lane.get("activation_id") or uuid4().hex)
        key = str(lane.get("lane_id") or lane_id)
        with self._active_lock, self._file_lock():
            current = self.read_active() or {}
            lanes = _normalize_lanes(current.get("lanes") or {})
            lanes[key] = {**lane, "lane_id": key, "activation_id": activation_id}
            self._write_json(
                self.active_path,
                {
                    "session_id": session_id,
                    "activation_id": str(current.get("activation_id") or activation_id),
                    "mode": "lanes",
                    "lanes": lanes,
                    "updated_at": time.time(),
                },
            )
        return activation_id

    def clear_active_lane(self, lane_id: str) -> None:
        """Remove one completed lane while preserving rules still active on other devices."""
        with self._active_lock, self._file_lock():
            current = self.read_active() or {}
            lanes = _normalize_lanes(current.get("lanes") or {})
            lanes.pop(str(lane_id), None)
            if not lanes:
                self.active_path.unlink(missing_ok=True)
                return
            self._write_json(self.active_path, {**current, "lanes": lanes, "updated_at": time.time()})

    def clear_active(self) -> None:
        """Clear active using the supplied state and inputs."""
        with self._active_lock, self._file_lock():
            self.active_path.unlink(missing_ok=True)

    def read_active(self) -> dict[str, Any] | None:
        """Read active using the supplied state and inputs."""
        return self._read_json(self.active_path) if self.active_path.exists() else None

    def set_probe(self, session_id: str, target: str, apis: list[Any]) -> None:
        """Persist probe using the supplied state and inputs."""
        self.set_probe_lanes(session_id, {target or "default": {"target": target, "apis": [api for api in apis if api], "client_ip": ""}})

    def set_probe_lanes(self, session_id: str, lanes: dict[str, dict[str, Any]]) -> None:
        """Persist probe lanes using the supplied state and inputs."""
        with self._active_lock, self._file_lock():
            self._write_json(
                self.probe_path,
                {
                    "session_id": session_id,
                    "mode": "lanes",
                    "lanes": _normalize_lanes(lanes),
                    "updated_at": time.time(),
                },
            )

    def clear_probe(self) -> None:
        """Clear probe using the supplied state and inputs."""
        with self._active_lock, self._file_lock():
            self.probe_path.unlink(missing_ok=True)

    def read_probe(self) -> dict[str, Any] | None:
        """Read probe using the supplied state and inputs."""
        return self._read_json(self.probe_path) if self.probe_path.exists() else None

    def record_hit(self, session_id: str, rule_id: str, evidence: dict[str, Any], lane_id: str = "") -> Path:
        """Record a redacted hit that proves mutation without retaining original field values."""
        path = self.hit_path(session_id, rule_id, lane_id=lane_id)
        with self._active_lock, self._file_lock():
            previous = self._read_json(path) if path.exists() else {}
            count = int(previous.get("request_count") or 0) + 1
            payload = {
                **_redact_evidence(evidence),
                "session_id": session_id,
                "rule_id": rule_id,
                "lane_id": lane_id or evidence.get("lane_id") or "",
                "request_count": count,
                "hit_at": time.time(),
            }
            self._write_json(path, payload)
        return path

    def read_hit(self, session_id: str, rule_id: str, lane_id: str = "") -> dict[str, Any] | None:
        """Read hit using the supplied state and inputs."""
        path = self.hit_path(session_id, rule_id, lane_id=lane_id)
        return self._read_json(path) if path.exists() else None

    def list_hits(self, session_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """List hits using the supplied state and inputs."""
        hits: list[dict[str, Any]] = []
        for path in self.hits_dir.glob("*.json"):
            payload = self._read_json(path)
            if session_id and payload.get("session_id") != session_id:
                continue
            hits.append(payload)
        hits.sort(key=lambda item: float(item.get("hit_at") or 0), reverse=True)
        return hits[: max(limit, 0)]

    def clear_hit(self, session_id: str, rule_id: str, lane_id: str = "") -> None:
        """Clear hit using the supplied state and inputs."""
        if lane_id:
            self.hit_path(session_id, rule_id, lane_id=lane_id).unlink(missing_ok=True)
            return
        for path in self.hits_dir.glob(f"{_safe_name(session_id)}_*_{_safe_name(rule_id)}.json"):
            path.unlink(missing_ok=True)
        self.hit_path(session_id, rule_id).unlink(missing_ok=True)

    def record_recent_request(self, session_id: str, request: dict[str, Any], limit: int = 30) -> None:
        """Record redacted request-routing evidence without retaining query values."""
        with self._active_lock, self._file_lock():
            rows = self.read_recent_requests(session_id="")
            rows.append({**_redact_evidence(request), "session_id": session_id, "recorded_at": time.time()})
            self._write_json(self.recent_requests_path, {"requests": rows[-limit:]})

    def read_recent_requests(self, session_id: str) -> list[dict[str, Any]]:
        """Read recent requests using the supplied state and inputs."""
        payload = self._read_json(self.recent_requests_path) if self.recent_requests_path.exists() else {}
        rows = payload.get("requests") or []
        return [row for row in rows if row.get("session_id") == session_id] if session_id else rows

    def clear_recent_requests(self, session_id: str) -> None:
        """Clear recent requests using the supplied state and inputs."""
        with self._active_lock, self._file_lock():
            rows = [row for row in self.read_recent_requests("") if row.get("session_id") != session_id]
            self._write_json(self.recent_requests_path, {"requests": rows})

    def record_event(self, session_id: str, event: dict[str, Any]) -> None:
        """Append one redacted proxy event to private JSONL evidence storage."""
        ensure_private_directory(self.proxy_dir)
        payload = {**_redact_evidence(event), "session_id": session_id, "event_at": time.time()}
        with self._active_lock, self._file_lock():
            append_private_text(self.events_path, json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def read_events(self, session_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
        """Read events using the supplied state and inputs."""
        if not self.events_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if session_id and payload.get("session_id") != session_id:
                continue
            rows.append(payload)
        return rows[-max(limit, 0) :]

    def record_modified_response(self, session_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
        """追加一次真实改写后的完整响应，并在写盘前清理敏感字段和 URL 参数。"""
        with self._active_lock, self._file_lock():
            existing = self._read_jsonl(self.modified_responses_path)
            lane_id = str(evidence.get("lane_id") or "")
            rule_id = str(evidence.get("rule_id") or "")
            activation_id = str(evidence.get("activation_id") or "")
            # sequence 只在同一 session/lane/rule/activation 内递增，报告可以稳定还原重复命中顺序。
            sequence = 1 + sum(
                1
                for row in existing
                if row.get("session_id") == session_id
                and row.get("lane_id") == lane_id
                and row.get("rule_id") == rule_id
                and row.get("activation_id") == activation_id
            )
            modified_response = _redact_sensitive(evidence.get("modified_response"))
            serialized = json.dumps(modified_response, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            max_bytes = _max_modified_response_bytes()
            if len(serialized) > max_bytes:
                modified_response = {
                    "_truncated": True,
                    "_original_bytes": len(serialized),
                    "_sha256": hashlib.sha256(serialized).hexdigest(),
                }
            payload = {
                **evidence,
                "id": str(evidence.get("id") or uuid4().hex),
                "session_id": session_id,
                "lane_id": lane_id,
                "rule_id": rule_id,
                "activation_id": activation_id,
                "sequence": sequence,
                "url": _redact_url(str(evidence.get("url") or "")),
                "modified_response": modified_response,
                "recorded_at": float(evidence.get("recorded_at") or time.time()),
            }
            append_private_text(
                self.modified_responses_path,
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            )
        return payload

    def read_modified_responses(
        self,
        session_id: str = "",
        lane_id: str = "",
        rule_id: str = "",
    ) -> list[dict[str, Any]]:
        """按执行关联字段读取全部改写响应，保持 mitmproxy 实际追加顺序。"""
        rows = self._read_jsonl(self.modified_responses_path)
        return [
            row
            for row in rows
            if (not session_id or row.get("session_id") == session_id)
            and (not lane_id or row.get("lane_id") == lane_id)
            and (not rule_id or row.get("rule_id") == rule_id)
        ]

    def hit_path(self, session_id: str, rule_id: str, lane_id: str = "") -> Path:
        """Handle hit path using the supplied state and inputs."""
        if lane_id:
            return self.hits_dir / f"{_safe_name(session_id)}_{_safe_name(lane_id)}_{_safe_name(rule_id)}.json"
        return self.hits_dir / f"{_safe_name(session_id)}_{_safe_name(rule_id)}.json"

    def _read_json(self, path: Path) -> dict[str, Any]:
        """Read json using the supplied state and inputs."""
        try:
            return json.loads(path.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError):
            return {}

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        """容错读取追加式 JSONL，单条损坏记录不会隐藏其他有效证据。"""
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return rows
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
        return rows

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        """Serialize proxy coordination state atomically with private permissions."""
        atomic_write_private_text(path, json.dumps(payload, ensure_ascii=False, indent=2))

    @contextlib.contextmanager
    def _file_lock(self):
        """Serialize read-modify-write state across threads and processes."""
        ensure_private_directory(self.lock_path.parent)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            self.lock_path.chmod(0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _normalize_lanes(lanes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Normalize lanes using the supplied state and inputs."""
    normalized: dict[str, dict[str, Any]] = {}
    for lane_id, lane in lanes.items():
        key = str(lane.get("lane_id") or lane_id)
        normalized[key] = {**lane, "lane_id": key}
    return normalized


def _safe_name(value: str) -> str:
    """Handle safe name using the supplied state and inputs."""
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value))
    return cleaned.strip("_") or "default"


def _max_modified_response_bytes() -> int:
    """Read the bounded response evidence limit, falling back safely on invalid input."""
    default = 10 * 1024 * 1024
    try:
        configured = int(os.environ.get("MOBILE_AUTO_MCP_MAX_RESPONSE_BYTES", str(default)))
    except ValueError:
        return default
    return configured if configured > 0 else default


_SENSITIVE_KEY_PARTS = {
    "token", "authorization", "cookie", "password", "secret", "session", "did",
    "id", "uid", "user", "name", "nickname", "phone", "mobile", "email", "address",
    "account", "device", "imei", "oaid", "idfa", "ip",
}


def _redact_sensitive(value: Any, key: str = "") -> Any:
    """递归保留 JSON 结构，并仅替换明确命中的敏感字段值。"""
    if key and _is_sensitive_key(key):
        return "***REDACTED***"
    if isinstance(value, dict):
        return {str(child_key): _redact_sensitive(child_value, str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    """按分隔符和 camelCase 拆分字段名，避免把 candidate 等普通键误判为 did。"""
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    parts = {part for part in re.split(r"[^a-z0-9]+", expanded.lower()) if part}
    return bool(parts & _SENSITIVE_KEY_PARTS)


def _redact_url(url: str) -> str:
    """Redact every URL query value while preserving keys needed for request-shape tracing."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        query = [(key, "***REDACTED***") for key, _ in parse_qsl(parts.query, keep_blank_values=True)]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    except ValueError:
        return url


_EVIDENCE_VALUE_KEYS = {"before", "after", "value", "request_body", "response_body", "headers", "cookies"}


def _redact_evidence(value: Any, key: str = "") -> Any:
    """Remove raw payload values from request/hit/event evidence before persistence."""
    normalized_key = str(key).lower()
    if normalized_key in {"url", "request_url"}:
        return _redact_url(str(value or ""))
    if normalized_key in _EVIDENCE_VALUE_KEYS or _is_sensitive_key(normalized_key):
        return "***REDACTED***"
    if isinstance(value, dict):
        return {str(child_key): _redact_evidence(child_value, str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_redact_evidence(item, key) for item in value]
    return value
