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
    """Lock byte zero, retrying Windows contention indefinitely for blocking callers."""
    if _msvcrt is None:  # pragma: no cover - protected by the OS-specific import above.
        raise RuntimeError("Windows file-lock backend is unavailable")
    original_offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        if os.fstat(descriptor).st_size == 0:
            # msvcrt cannot lock beyond EOF, so reserve byte zero before the first acquisition.
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                os.write(descriptor, b"\0")
            except OSError as exc:
                # A concurrent creator can seed and lock byte zero after the size check. In that
                # race, proceed to the normal lock loop instead of treating contention as I/O loss.
                if not _is_contention(exc):
                    raise
        mode = _msvcrt.LK_LOCK if blocking else _msvcrt.LK_NBLCK
        while True:
            # Every retry repositions explicitly because msvcrt locks from the descriptor's current offset.
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                _msvcrt.locking(descriptor, mode, 1)
                return
            except OSError as exc:
                if not _is_contention(exc):
                    raise
                if not blocking:
                    raise BlockingIOError(exc.errno, exc.strerror) from exc
                # LK_LOCK retries only for a bounded window internally; repeat until ownership is available.
                continue
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
    if not blocking and _is_contention(exc):
        raise BlockingIOError(exc.errno, exc.strerror) from exc
    raise exc


def _is_contention(exc: OSError) -> bool:
    """Recognize errno values emitted when an advisory lock is already owned."""
    return exc.errno in {errno.EACCES, errno.EDEADLK}
