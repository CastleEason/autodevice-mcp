"""Cross-platform advisory file locks backed only by the Python standard library."""

from __future__ import annotations

import errno
import os
from typing import Any


_IS_WINDOWS = os.name == "nt"

# Import only the backend provided by the running OS so package import remains portable.
if _IS_WINDOWS:
    import msvcrt as _msvcrt

    _fcntl: Any | None = None
else:
    import fcntl as _fcntl

    _msvcrt: Any | None = None


def lock_file(descriptor: int, blocking: bool = True) -> None:
    """Acquire an exclusive advisory lock, optionally failing immediately on contention."""
    if _IS_WINDOWS:
        _lock_windows(descriptor, blocking=blocking)
        return
    if _fcntl is None:  # pragma: no cover - protected by the OS-specific import above.
        raise RuntimeError("POSIX file-lock backend is unavailable")
    operation = _fcntl.LOCK_EX | (0 if blocking else _fcntl.LOCK_NB)
    try:
        _fcntl.flock(descriptor, operation)
    except OSError as exc:
        _raise_nonblocking_contention(exc, blocking=blocking)


def unlock_file(descriptor: int) -> None:
    """Release an advisory lock previously acquired for the descriptor."""
    if _IS_WINDOWS:
        _unlock_windows(descriptor)
        return
    if _fcntl is None:  # pragma: no cover - protected by the OS-specific import above.
        raise RuntimeError("POSIX file-lock backend is unavailable")
    _fcntl.flock(descriptor, _fcntl.LOCK_UN)


def _lock_windows(descriptor: int, *, blocking: bool) -> None:
    """Lock byte zero with msvcrt after ensuring an empty lock file has one byte."""
    if _msvcrt is None:  # pragma: no cover - protected by the OS-specific import above.
        raise RuntimeError("Windows file-lock backend is unavailable")
    original_offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        if os.fstat(descriptor).st_size == 0:
            # msvcrt cannot lock beyond EOF, so reserve byte zero before the first acquisition.
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        mode = _msvcrt.LK_LOCK if blocking else _msvcrt.LK_NBLCK
        try:
            _msvcrt.locking(descriptor, mode, 1)
        except OSError as exc:
            _raise_nonblocking_contention(exc, blocking=blocking)
    finally:
        os.lseek(descriptor, original_offset, os.SEEK_SET)


def _unlock_windows(descriptor: int) -> None:
    """Release the single Windows byte-range used by the lock adapter."""
    if _msvcrt is None:  # pragma: no cover - protected by the OS-specific import above.
        raise RuntimeError("Windows file-lock backend is unavailable")
    original_offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        _msvcrt.locking(descriptor, _msvcrt.LK_UNLCK, 1)
    finally:
        os.lseek(descriptor, original_offset, os.SEEK_SET)


def _raise_nonblocking_contention(exc: OSError, *, blocking: bool) -> None:
    """Normalize Windows/POSIX contention errno values for non-blocking callers."""
    if not blocking and exc.errno in {errno.EACCES, errno.EDEADLK}:
        raise BlockingIOError(exc.errno, exc.strerror) from exc
    raise exc
