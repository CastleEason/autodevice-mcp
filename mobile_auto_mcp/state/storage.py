"""Local JSON storage for rules, runs, screenshots, reports, and proxy state."""

from __future__ import annotations

import json
import os
import fcntl
import threading
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import uuid4

from mobile_auto_mcp.proxy.proxy_state import ProxyState
from mobile_auto_mcp.state.private_files import atomic_write_private_text, ensure_private_directory


_STORE_LOCK = threading.RLock()
_TRANSACTION_LOCAL = threading.local()
_RUN_STATUSES = {"pending_review", "invalid_execution", "passed", "failed", "needs_check"}


def default_home() -> Path:
    """Return the default MCP data home."""
    return Path(os.environ.get("MOBILE_AUTO_MCP_HOME", "~/.mobile_auto_mcp")).expanduser()


def workspace_home(
    base_home: str | Path | None = None,
    tenant_id: str = "",
    workspace_id: str = "",
    app_id: str = "",
) -> Path:
    """Build a tenant/workspace/app isolated storage path."""
    root = Path(base_home).expanduser() if base_home else default_home()
    tenant = _safe_segment(tenant_id or "local")
    workspace = _safe_segment(workspace_id or "default")
    home = root / "tenants" / tenant / "workspaces" / workspace
    if app_id:
        home = home / "apps" / _safe_segment(app_id)
    return home


def now() -> str:
    """Return an ISO timestamp without sub-second noise."""
    return datetime.now().isoformat(timespec="seconds")


def _safe_segment(value: str) -> str:
    """Handle safe segment using the supplied state and inputs."""
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value)).strip("_")
    return cleaned or "default"


def _workspace_mutation(method: Any) -> Any:
    """Serialize one read-modify-write operation across threads and MCP processes."""
    @wraps(method)
    def guarded(self: "LocalStore", *args: Any, **kwargs: Any) -> Any:
        """Execute the wrapped mutation while holding its workspace file lock."""
        path = str((self.home / "workspace_store.lock").resolve())
        with _STORE_LOCK:
            held = getattr(_TRANSACTION_LOCAL, "held", {})
            if path in held:
                descriptor, depth = held[path]
                held[path] = (descriptor, depth + 1)
                try:
                    return method(self, *args, **kwargs)
                finally:
                    held[path] = (descriptor, depth)
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            held[path] = (descriptor, 1)
            _TRANSACTION_LOCAL.held = held
            try:
                return method(self, *args, **kwargs)
            finally:
                held.pop(path, None)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    return guarded


class LocalStore:
    """Small JSON store used by the MCP server and mitmproxy addon."""

    def __init__(self, home: str | Path | None = None) -> None:
        """Initialize LocalStore state, configuration, and runtime dependencies."""
        self.home = ensure_private_directory(Path(home).expanduser() if home else default_home())
        ensure_private_directory(self.home / "screenshots")
        ensure_private_directory(self.home / "reports")
        ensure_private_directory(self.home / "reviews")
        self.rules_path = self.home / "rules.json"
        self.runs_path = self.home / "runs.json"
        self.proxy_state = ProxyState(self.home)

    def list_rules(self, keyword: str = "", enabled_only: bool = True) -> list[dict[str, Any]]:
        """List rules using the supplied state and inputs."""
        rules = self._read_list(self.rules_path)
        return [rule for rule in rules if self._matches_rule(rule, keyword, enabled_only)]

    @_workspace_mutation
    def save_rules(self, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Persist rules using the supplied state and inputs."""
        with _STORE_LOCK:
            existing = self._read_list(self.rules_path)
            index_by_id = {str(rule.get("id") or ""): index for index, rule in enumerate(existing) if rule.get("id")}
            saved: list[dict[str, Any]] = []
            for rule in rules:
                rule_id = rule.get("id") or uuid4().hex
                previous = existing[index_by_id[rule_id]] if rule_id in index_by_id else {}
                record = {
                    "id": rule_id,
                    "case_name": rule.get("case_name") or "未命名规则",
                    "api": rule.get("api") or "",
                    "host": rule.get("host") or previous.get("host") or "",
                    "method": str(rule.get("method") or rule.get("http_method") or previous.get("method") or "").upper(),
                    "mutations": rule.get("mutations") or [],
                    "patches": rule.get("patches") or [],
                    "fixtures": rule.get("fixtures") or [],
                    "mock_sources": rule.get("mock_sources") or [],
                    "expected": rule.get("expected") or "",
                    "source_feature": rule.get("source_feature") or "未分组需求",
                    "source_module": rule.get("source_module") or "",
                    "source_file": rule.get("source_file") or previous.get("source_file") or "",
                    "source_line": rule.get("source_line") or previous.get("source_line") or 0,
                    "enabled": bool(rule.get("enabled", True)),
                    "created_at": rule.get("created_at") or previous.get("created_at") or now(),
                }
                if rule_id in index_by_id:
                    existing[index_by_id[rule_id]] = record
                else:
                    index_by_id[rule_id] = len(existing)
                    existing.append(record)
                saved.append(record)
            self._write_list(self.rules_path, existing)
            return saved

    @_workspace_mutation
    def merge_rule_assets(
        self,
        rule_ids: list[str],
        patches: list[dict[str, Any]] | None = None,
        fixtures: list[dict[str, Any]] | None = None,
        mock_sources: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Handle merge rule assets using the supplied state and inputs."""
        id_set = set(rule_ids)
        rules = self._read_list(self.rules_path)
        updated: list[dict[str, Any]] = []
        for rule in rules:
            if rule.get("id") not in id_set:
                continue
            if patches:
                rule["patches"] = [*(rule.get("patches") or []), *patches]
            if fixtures:
                rule["fixtures"] = [*(rule.get("fixtures") or []), *fixtures]
            if mock_sources:
                rule["mock_sources"] = [*(rule.get("mock_sources") or []), *mock_sources]
            updated.append(rule)
        if updated:
            self._write_list(self.rules_path, rules)
        return updated

    @_workspace_mutation
    def update_rule_apis(self, rule_ids: list[str], api_override: str = "", api_overrides: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Update rule apis using the supplied state and inputs."""
        id_set = set(rule_ids)
        override = (api_override or "").strip()
        mappings = {str(k).strip(): str(v).strip() for k, v in (api_overrides or {}).items() if str(k).strip() and str(v).strip()}
        if not override and not mappings:
            return []
        rules = self._read_list(self.rules_path)
        updated: list[dict[str, Any]] = []
        for rule in rules:
            if rule.get("id") not in id_set:
                continue
            current = str(rule.get("api") or "").strip()
            next_api = mappings.get(current) or override
            if next_api and next_api != current:
                rule["original_api"] = rule.get("original_api") or current
                rule["api"] = next_api
                updated.append(rule)
        if updated:
            self._write_list(self.rules_path, rules)
        return updated

    @_workspace_mutation
    def update_rule_request_contracts(
        self,
        rule_ids: list[str],
        *,
        api_override: str = "",
        api_overrides: dict[str, str] | None = None,
        host_override: str = "",
        host_overrides: dict[str, str] | None = None,
        method_override: str = "",
        method_overrides: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Update exact API, host, and method contracts for selected stored rules."""
        id_set = {str(value) for value in rule_ids}
        api_map = {str(key): str(value).strip() for key, value in (api_overrides or {}).items() if str(value).strip()}
        host_map = {str(key): str(value).strip() for key, value in (host_overrides or {}).items() if str(value).strip()}
        method_map = {str(key): str(value).strip().upper() for key, value in (method_overrides or {}).items() if str(value).strip()}
        rules = self._read_list(self.rules_path)
        updated: list[dict[str, Any]] = []
        for rule in rules:
            rule_id = str(rule.get("id") or "")
            if rule_id not in id_set:
                continue
            current_api = str(rule.get("api") or "")
            next_api = api_map.get(rule_id) or api_map.get(current_api) or api_override.strip() or current_api
            next_host = host_map.get(rule_id) or host_override.strip() or str(rule.get("host") or "")
            next_method = method_map.get(rule_id) or method_override.strip().upper() or str(rule.get("method") or "").upper()
            if next_api != current_api:
                rule["original_api"] = rule.get("original_api") or current_api
            rule.update({"api": next_api, "host": next_host, "method": next_method})
            updated.append(rule)
        if updated:
            self._write_list(self.rules_path, rules)
        return updated

    @_workspace_mutation
    def start_session(self, target: str, rule_ids: list[str]) -> dict[str, Any]:
        """Create a formal execution session after readiness has succeeded."""
        state = self._read_runs()
        session = {
            "session_id": uuid4().hex,
            "target": target,
            "rule_ids": rule_ids,
            "status": "created",
            "started_at": now(),
            "finished_at": "",
        }
        state["sessions"].insert(0, session)
        self._write_runs(state)
        return session

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return formal sessions in newest-first order for audit and readiness-order checks."""
        return self._read_runs()["sessions"]

    def get_session(self, session_id: str) -> dict[str, Any]:
        """Return one formal session with lifecycle metadata, or an empty mapping when absent."""
        return next((session for session in self.list_sessions() if session.get("session_id") == session_id), {})

    @_workspace_mutation
    def update_session_metadata(self, session_id: str, **metadata: Any) -> dict[str, Any]:
        """Merge lifecycle and audit metadata into one formal session record."""
        state = self._read_runs()
        for session in state["sessions"]:
            if session["session_id"] == session_id:
                session.update(metadata)
                self._write_runs(state)
                return session
        raise KeyError(session_id)

    @_workspace_mutation
    def record_run_result(
        self,
        session_id: str,
        target: str,
        rule_id: str,
        status: str = "pending_review",
        screenshot: str = "",
        screenshots: list[str] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        review_note: str = "",
        execution_gate: dict[str, Any] | None = None,
        traceability: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist one lane result after validating its report-facing status vocabulary."""
        _validate_run_status(status)
        with _STORE_LOCK:
            rule = next((item for item in self.list_rules(enabled_only=False) if item["id"] == rule_id), {})
            state = self._read_runs()
            run = {
                "id": uuid4().hex,
                "session_id": session_id,
                "target": target,
                "rule_id": rule_id,
                "case_name": rule.get("case_name") or "未命名规则",
                "api": rule.get("api") or "",
                "host": rule.get("host") or "",
                "method": rule.get("method") or "",
                "mutations": rule.get("mutations") or [],
                "expected": rule.get("expected") or "",
                "source_feature": rule.get("source_feature") or "",
                "source_module": rule.get("source_module") or "",
                "status": status,
                "screenshot": screenshot,
                "screenshots": screenshots or ([screenshot] if screenshot else []),
                "evidence": evidence or [],
                "execution_gate": execution_gate or {},
                "traceability": traceability or {},
                "review_note": review_note,
                "visual_precheck": {},
                "visual_review": {},
                "created_at": now(),
            }
            state["runs"].insert(0, run)
            self._write_runs(state)
            return run

    @_workspace_mutation
    def update_run_review(self, run_id: str, status: str, review_note: str = "", visual_review: dict[str, Any] | None = None) -> dict[str, Any]:
        """Apply a validated review decision to one existing run record."""
        _validate_run_status(status)
        state = self._read_runs()
        for run in state["runs"]:
            if run.get("id") == run_id:
                if run.get("status") == "invalid_execution":
                    raise ValueError("invalid_execution 缺少可复核执行证据，禁止提升为最终审查结论")
                if status in {"passed", "failed"}:
                    gate = run.get("execution_gate") or {}
                    page_anchor = gate.get("page_anchor") or {}
                    if not (
                        gate.get("change_applied")
                        and page_anchor.get("ok")
                        and page_anchor.get("verified")
                        and not page_anchor.get("skipped")
                    ):
                        raise ValueError("最终审查要求已改写响应和已验证且未跳过的页面锚点")
                run["status"] = status
                run["review_note"] = review_note
                if visual_review is not None:
                    run["visual_review"] = visual_review
                run["reviewed_at"] = now()
                self._write_runs(state)
                return run
        raise KeyError(run_id)

    @_workspace_mutation
    def update_run_visual_precheck(self, run_id: str, visual_precheck: dict[str, Any]) -> dict[str, Any]:
        """Persist algorithmic screenshot evidence without changing the run's final review status."""
        state = self._read_runs()
        for run in state["runs"]:
            if run.get("id") == run_id:
                run["visual_precheck"] = dict(visual_precheck)
                run["visual_prechecked_at"] = now()
                self._write_runs(state)
                return run
        raise KeyError(run_id)

    @_workspace_mutation
    def update_manual_review(self, run_id: str, status: str, review_note: str = "", reviewer: str = "human") -> dict[str, Any]:
        """Update manual review using the supplied state and inputs."""
        if status not in {"passed", "failed", "needs_check"}:
            raise ValueError(f"不支持的人工审查状态: {status}")
        return self.update_run_review(run_id, status, review_note, {"reviewer": reviewer, "manual": True})

    def list_runs(self, session_id: str = "") -> list[dict[str, Any]]:
        """List runs using the supplied state and inputs."""
        runs = self._read_runs()["runs"]
        return [run for run in runs if run.get("session_id") == session_id] if session_id else runs

    def session_summary(self, session_id: str) -> dict[str, Any]:
        """Handle session summary using the supplied state and inputs."""
        runs = self.list_runs(session_id)
        return {
            "session_id": session_id,
            "records": len(runs),
            "pending_review": sum(1 for r in runs if r.get("status") == "pending_review"),
            "invalid_execution": sum(1 for r in runs if r.get("status") == "invalid_execution"),
            "passed": sum(1 for r in runs if r.get("status") == "passed"),
            "failed": sum(1 for r in runs if r.get("status") == "failed"),
            "needs_check": sum(1 for r in runs if r.get("status") == "needs_check"),
            "screenshots": sum(1 for r in runs if r.get("screenshot")),
        }

    @_workspace_mutation
    def update_session_status(self, session_id: str, status: str) -> dict[str, Any]:
        """Set session status and close every terminal state with a finished timestamp."""
        state = self._read_runs()
        for session in state["sessions"]:
            if session["session_id"] == session_id:
                session["status"] = status
                if status in {"finished", "reviewed", "blocked", "partial", "cleanup_failed", "failed"}:
                    session["finished_at"] = now()
                self._write_runs(state)
                return session
        raise KeyError(session_id)

    @_workspace_mutation
    def refresh_session_review_status(self, session_id: str) -> dict[str, Any]:
        """Derive the session state only from auditable execution gates and explicit final reviews."""
        runs = self.list_runs(session_id)
        statuses = [str(run.get("status") or "") for run in runs]
        if not runs or any(status == "invalid_execution" for status in statuses):
            status, review_complete = "blocked", False
        elif all(item == "passed" for item in statuses):
            status, review_complete = "reviewed", True
        elif all(item in {"passed", "failed"} for item in statuses) and any(item == "failed" for item in statuses):
            status, review_complete = "failed", True
        else:
            status, review_complete = "awaiting_review", False
        session = self.update_session_metadata(
            session_id,
            status=status,
            review_complete=review_complete,
            review_statuses=statuses,
            **({"finished_at": now()} if review_complete or status == "blocked" else {}),
        )
        return session

    def _matches_rule(self, rule: dict[str, Any], keyword: str, enabled_only: bool) -> bool:
        """Return whether rule using the supplied state and inputs."""
        if enabled_only and not rule.get("enabled", True):
            return False
        keyword = (keyword or "").strip()
        if not keyword:
            return True
        haystack = " ".join(str(rule.get(k) or "") for k in ("id", "case_name", "api", "source_feature", "source_module", "expected"))
        return keyword in haystack

    def _read_runs(self) -> dict[str, Any]:
        """Read runs using the supplied state and inputs."""
        if not self.runs_path.exists():
            return {"sessions": [], "runs": []}
        try:
            payload = json.loads(self.runs_path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return {"sessions": [], "runs": []}
        payload.setdefault("sessions", [])
        payload.setdefault("runs", [])
        return payload

    def _write_runs(self, payload: dict[str, Any]) -> None:
        """Write runs using the supplied state and inputs."""
        self._write_json(self.runs_path, payload)

    def _read_list(self, path: Path) -> list[dict[str, Any]]:
        """Read list using the supplied state and inputs."""
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8") or "[]")
        except json.JSONDecodeError:
            return []
        return payload if isinstance(payload, list) else []

    def _write_list(self, path: Path, rows: list[dict[str, Any]]) -> None:
        """Write list using the supplied state and inputs."""
        self._write_json(path, rows)

    def _write_json(self, path: Path, payload: Any) -> None:
        """Serialize JSON atomically with owner-only permissions."""
        atomic_write_private_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _validate_run_status(status: str) -> None:
    """Reject states that cannot be rendered or summarized by the public report contract."""
    if status not in _RUN_STATUSES:
        raise ValueError(f"不支持的执行状态: {status}")
