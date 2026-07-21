"""Safe argv-based execution for explicitly configured external helpers."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Mapping


def render_argv(template: str, values: Mapping[str, object] | None = None) -> list[str]:
    """Parse a trusted command template first, then substitute values without invoking a shell."""
    tokens = shlex.split(str(template or ""))
    if not tokens:
        raise ValueError("configured command is empty")
    substitutions = dict(values or {})
    try:
        # Formatting after tokenization keeps spaces and shell metacharacters inside one argv element.
        return [token.format(**substitutions) for token in tokens]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"configured command contains an invalid placeholder: {exc}") from exc


def run_configured_command(
    template: str,
    *,
    values: Mapping[str, object] | None = None,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute one configured helper as argv so runtime text cannot change command semantics."""
    argv = render_argv(template, values)
    return subprocess.run(
        argv,
        text=True,
        capture_output=True,
        timeout=float(timeout),
        env=dict(env) if env is not None else None,
        check=False,
    )
