"""Cross-platform tests for private runtime file primitives."""

from __future__ import annotations

from pathlib import Path

import pytest

from mobile_auto_mcp.state import private_files


def test_private_text_writes_work_when_fchmod_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep Windows-compatible writes functional when descriptor chmod is unavailable."""
    destination = tmp_path / "state" / "events.jsonl"
    monkeypatch.delattr(private_files.os, "fchmod", raising=False)

    private_files.atomic_write_private_text(destination, "first\n")
    private_files.append_private_text(destination, "second\n")

    assert destination.read_text(encoding="utf-8") == "first\nsecond\n"
