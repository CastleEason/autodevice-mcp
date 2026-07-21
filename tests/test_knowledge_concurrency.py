"""Regression tests for cross-process knowledge-base mutations."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from mobile_auto_mcp.state.knowledge import KnowledgeBase


def test_field_alias_updates_do_not_get_lost_across_processes(tmp_path: Path) -> None:
    """验证多个 MCP 进程并发学习别名时不会由后写进程覆盖另一批记录。"""
    scripts = [
        (
            "from mobile_auto_mcp.state.knowledge import KnowledgeBase; "
            f"kb=KnowledgeBase({str(tmp_path)!r}); "
            f"[kb.record_field_alias('app','{prefix}-'+str(i),'field') for i in range(20)]"
        )
        for prefix in ("left", "right")
    ]
    processes = [subprocess.Popen([sys.executable, "-c", script]) for script in scripts]

    assert [process.wait(timeout=10) for process in processes] == [0, 0]
    aliases = KnowledgeBase(tmp_path)._read()["field_aliases"]["app"]
    assert len(aliases) == 40
