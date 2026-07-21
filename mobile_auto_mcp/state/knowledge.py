"""Knowledge-base helpers for reusable navigation and field aliases."""

from __future__ import annotations

import hashlib
import json
import fcntl
import os
from pathlib import Path
from functools import wraps
from typing import Any

from mobile_auto_mcp.state.private_files import atomic_write_private_text, ensure_private_directory


def _knowledge_mutation(method: Any) -> Any:
    """Serialize one knowledge read-modify-write operation across MCP processes."""
    @wraps(method)
    def guarded(self: "KnowledgeBase", *args: Any, **kwargs: Any) -> Any:
        """Execute a knowledge mutation while holding its advisory file lock."""
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            return method(self, *args, **kwargs)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    return guarded


class KnowledgeBase:
    """Persist reusable paths under a user-provided knowledge directory."""

    def __init__(self, directory: str | Path) -> None:
        """Initialize KnowledgeBase state, configuration, and runtime dependencies."""
        self.directory = ensure_private_directory(Path(directory).expanduser())
        self.path = self.directory / "mobile_auto_mcp_knowledge.json"
        self.lock_path = self.directory / "mobile_auto_mcp_knowledge.lock"

    @_knowledge_mutation
    def record_navigation_path(
        self,
        app_id: str,
        target_page: str,
        path: list[dict[str, Any]],
        *,
        target: str = "",
        context: str = "",
    ) -> dict[str, Any]:
        """按应用、设备端和页面上下文保存路径，避免跨端或跨实体复用。"""
        data = self._read()
        paths = data.setdefault("navigation_paths", {}).setdefault(app_id, {})
        storage_key = _navigation_storage_key(target_page, target, context)
        existing = paths.get(storage_key)
        if existing == path:
            return {
                "app_id": app_id,
                "target_page": target_page,
                "target": target,
                "context_fingerprint": _context_fingerprint(context),
                "path": path,
                "status": "skipped",
                "reason": "duplicate",
                "saved": False,
            }
        paths[storage_key] = path
        self._write(data)
        return {
            "app_id": app_id,
            "target_page": target_page,
            "target": target,
            "context_fingerprint": _context_fingerprint(context),
            "path": path,
            "status": "saved",
            "saved": True,
        }

    def suggest_navigation_path(self, app_id: str, target_page: str, *, target: str = "", context: str = "") -> dict[str, Any]:
        """只返回作用域完全匹配的导航路径，不对其他端或上下文做模糊回退。"""
        data = self._read()
        storage_key = _navigation_storage_key(target_page, target, context)
        path = data.get("navigation_paths", {}).get(app_id, {}).get(storage_key, [])
        return {
            "app_id": app_id,
            "target_page": target_page,
            "target": target,
            "context_fingerprint": _context_fingerprint(context),
            "path": path,
        }

    @_knowledge_mutation
    def record_request_trigger_path(
        self,
        app_id: str,
        target_page: str,
        path: list[dict[str, Any]],
        *,
        target: str = "",
        context: str = "",
        request_hit: bool,
        page_verified: bool,
    ) -> dict[str, Any]:
        """Persist a trigger only after both the request and page are verified."""
        if not request_hit or not page_verified:
            return {
                "app_id": app_id,
                "target_page": target_page,
                "target": target,
                "path": path,
                "status": "skipped",
                "reason": "verification_failed",
                "saved": False,
            }
        data = self._read()
        paths = data.setdefault("request_trigger_paths", {}).setdefault(app_id, {})
        storage_key = _navigation_storage_key(target_page, target, context)
        if paths.get(storage_key) == path:
            return {"app_id": app_id, "target_page": target_page, "target": target, "path": path, "status": "skipped", "reason": "duplicate", "saved": False}
        paths[storage_key] = path
        self._write(data)
        return {"app_id": app_id, "target_page": target_page, "target": target, "path": path, "status": "saved", "saved": True}

    def suggest_request_trigger_path(self, app_id: str, target_page: str, *, target: str = "", context: str = "") -> dict[str, Any]:
        """Return scoped request trigger path using the supplied state and inputs."""
        data = self._read()
        storage_key = _navigation_storage_key(target_page, target, context)
        return {
            "app_id": app_id,
            "target_page": target_page,
            "target": target,
            "context_fingerprint": _context_fingerprint(context),
            "path": data.get("request_trigger_paths", {}).get(app_id, {}).get(storage_key, []),
        }

    @_knowledge_mutation
    def record_field_alias(self, app_id: str, alias: str, field: str) -> dict[str, Any]:
        """Record field alias using the supplied state and inputs."""
        data = self._read()
        data.setdefault("field_aliases", {}).setdefault(app_id, {})[alias] = field
        self._write(data)
        return {"app_id": app_id, "alias": alias, "field": field}

    def suggest_field_alias(self, app_id: str, alias: str) -> dict[str, Any]:
        """Return scoped field alias using the supplied state and inputs."""
        data = self._read()
        return {"app_id": app_id, "alias": alias, "field": data.get("field_aliases", {}).get(app_id, {}).get(alias, "")}

    def _read(self) -> dict[str, Any]:
        """Handle read using the supplied state and inputs."""
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        """通过临时文件原子替换知识文件，避免中途写入留下损坏 JSON。"""
        atomic_write_private_text(self.path, json.dumps(data, ensure_ascii=False, indent=2))


def _navigation_storage_key(target_page: str, target: str, context: str) -> str:
    """生成稳定作用域键；空作用域继续兼容既有知识文件。"""
    if not target and not context:
        return target_page
    return f"{target or 'default'}::{_context_fingerprint(context) or 'default'}::{target_page}"


def _context_fingerprint(context: str) -> str:
    """对运行时页面上下文取短指纹，避免把业务实体直接写入知识键。"""
    normalized = " ".join((context or "").strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16] if normalized else ""
