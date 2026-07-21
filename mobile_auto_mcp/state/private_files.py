"""Private filesystem primitives for runtime evidence and lifecycle state."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _set_private_descriptor_mode(descriptor: int) -> None:
    """Apply owner-only POSIX permissions when the host exposes descriptor chmod."""
    fchmod = getattr(os, "fchmod", None)
    if callable(fchmod):
        fchmod(descriptor, 0o600)


def ensure_private_directory(path: str | Path) -> Path:
    """Create a directory tree and force the requested leaf to owner-only access."""
    directory = Path(path).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def atomic_write_private_text(path: str | Path, text: str) -> Path:
    """Atomically replace a text file while enforcing owner-only read/write access."""
    destination = Path(path).expanduser()
    ensure_private_directory(destination.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        _set_private_descriptor_mode(descriptor)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
        destination.chmod(0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return destination


def append_private_text(path: str | Path, text: str) -> Path:
    """Append text to an owner-only file and repair permissions on pre-existing files."""
    destination = Path(path).expanduser()
    ensure_private_directory(destination.parent)
    descriptor = os.open(destination, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        _set_private_descriptor_mode(descriptor)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return destination


def resolve_within(root: str | Path, candidate: str | Path, *, must_exist: bool = False) -> Path:
    """Resolve a path and reject traversal or symlink escape outside an authorized root."""
    trusted_root = Path(root).expanduser().resolve(strict=must_exist)
    resolved = Path(candidate).expanduser().resolve(strict=must_exist)
    try:
        resolved.relative_to(trusted_root)
    except ValueError as exc:
        raise ValueError(f"path escapes authorized root: {candidate}") from exc
    return resolved
