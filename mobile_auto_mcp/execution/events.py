"""Append-only execution events and bounded lane stage state."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from mobile_auto_mcp.state.private_files import append_private_text, ensure_private_directory


class LaneStageTimeout(TimeoutError):
    """Raised when one lane operation exceeds its configured stage budget."""

    def __init__(self, stage: str, budget_seconds: float, elapsed_seconds: float) -> None:
        """Initialize LaneStageTimeout state, configuration, and runtime dependencies."""
        self.stage = stage
        self.budget_seconds = budget_seconds
        self.elapsed_seconds = elapsed_seconds
        super().__init__(f"stage {stage} exceeded {budget_seconds:g}s budget")

    def as_dict(self) -> dict[str, Any]:
        """Serialize this result into a JSON-compatible dictionary."""
        return {
            "stage": self.stage,
            "budget_seconds": self.budget_seconds,
            "elapsed_seconds": round(self.elapsed_seconds, 6),
        }


class ExecutionEventStore:
    """Persist lane progress as JSONL so blocked stages remain observable."""

    def __init__(self, home: str | Path) -> None:
        """Initialize ExecutionEventStore state, configuration, and runtime dependencies."""
        self.path = Path(home) / "execution" / "events.jsonl"
        ensure_private_directory(self.path.parent)
        self._lock = threading.RLock()

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        """Append one structured event to private evidence storage."""
        row = {**event, "event_at": time.time()}
        with self._lock:
            append_private_text(self.path, json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return row

    def read(self, session_id: str = "", lane_id: str = "", limit: int = 500) -> list[dict[str, Any]]:
        """Read matching persisted records with an optional result limit."""
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if session_id and row.get("session_id") != session_id:
                continue
            if lane_id and row.get("lane_id") != lane_id:
                continue
            rows.append(row)
        return rows[-max(0, limit) :]


class LaneStateMachine:
    """Record one lane's stage lifecycle without coordinating other lanes."""

    def __init__(self, events: ExecutionEventStore, session_id: str, lane_id: str) -> None:
        """Initialize LaneStateMachine state, configuration, and runtime dependencies."""
        self.events = events
        self.session_id = session_id
        self.lane_id = lane_id
        self.current_stage = ""

    def _record(self, event: str, stage: str, **data: Any) -> dict[str, Any]:
        """Handle record using the supplied state and inputs."""
        self.current_stage = stage
        return self.events.append(
            {"event": event, "session_id": self.session_id, "lane_id": self.lane_id, "stage": stage, **data}
        )

    def start(self, stage: str, budget_seconds: float, **data: Any) -> dict[str, Any]:
        """Record the start of a bounded lane stage."""
        return self._record("stage_started", stage, budget_seconds=budget_seconds, **data)

    def retry(self, stage: str, code: str, attempt: int) -> dict[str, Any]:
        """Record a retry attempt for a bounded lane stage."""
        return self._record("stage_retry", stage, code=code, attempt=attempt)

    def finish(self, stage: str, ok: bool, code: str = "", **data: Any) -> dict[str, Any]:
        """Record the terminal result of a bounded lane stage."""
        return self._record("stage_finished", stage, ok=ok, code=code, **data)

    def run(
        self,
        stage: str,
        budget_seconds: float,
        operation: Any,
        *,
        failure_code: str = "stage_operation_failed",
        **data: Any,
    ) -> Any:
        """Run one operation to completion, then enforce its budget before shared-state cleanup."""
        budget = max(0.001, float(budget_seconds))
        self.start(stage, budget_seconds=budget, **data)
        started = time.monotonic()
        try:
            value = operation()
        except BaseException as exc:
            elapsed = time.monotonic() - started
            if elapsed > budget:
                self._record(
                    "stage_timeout",
                    stage,
                    ok=False,
                    code="stage_timeout",
                    budget_seconds=budget,
                    elapsed_seconds=round(elapsed, 6),
                    operation_error=str(exc),
                    **data,
                )
                raise LaneStageTimeout(stage, budget, elapsed) from exc
            self.finish(stage, ok=False, code=failure_code, elapsed_seconds=round(elapsed, 6), error=str(exc), **data)
            raise
        elapsed = time.monotonic() - started
        if elapsed > budget:
            # A Python thread cannot be killed safely. Synchronous completion guarantees no late action
            # can mutate a later rule after the timeout has been returned to the caller.
            self._record(
                "stage_timeout",
                stage,
                ok=False,
                code="stage_timeout",
                budget_seconds=budget,
                elapsed_seconds=round(elapsed, 6),
                **data,
            )
            raise LaneStageTimeout(stage, budget, elapsed)
        if isinstance(value, dict) and "ok" in value:
            ok = bool(value.get("ok"))
        elif hasattr(value, "ok"):
            ok = bool(value.ok)
        else:
            ok = True
        self.finish(
            stage,
            ok=ok,
            code="" if ok else failure_code,
            elapsed_seconds=round(elapsed, 6),
            **data,
        )
        return value
