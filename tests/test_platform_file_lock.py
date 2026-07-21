"""Portability and regression tests for advisory file-lock boundaries."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mobile_auto_mcp.platform import file_lock
from mobile_auto_mcp.platform.file_lock import lock_file, unlock_file
from mobile_auto_mcp.state.knowledge import KnowledgeBase
from mobile_auto_mcp.state.storage import LocalStore


class _FakeFcntl:
    """Record POSIX flock operations without acquiring a real process lock."""

    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8

    def __init__(self) -> None:
        """Start with no recorded descriptor or operation flags."""
        self.calls: list[tuple[int, int]] = []

    def flock(self, descriptor: int, operation: int) -> None:
        """Capture the exact operation passed to the injected POSIX backend."""
        self.calls.append((descriptor, operation))


class _FakeMsvcrt:
    """Record Windows byte-range locking while exposing stdlib-compatible constants."""

    LK_LOCK = 1
    LK_NBLCK = 2
    LK_UNLCK = 3

    def __init__(self, error: OSError | None = None) -> None:
        """Optionally fail acquisition to characterize Windows contention mapping."""
        self.error = error
        self.calls: list[tuple[int, int, int, int]] = []

    def locking(self, descriptor: int, mode: int, byte_count: int) -> None:
        """Record file size and lock arguments at the moment byte zero is locked."""
        self.calls.append((descriptor, mode, byte_count, os.fstat(descriptor).st_size))
        if self.error is not None and mode != self.LK_UNLCK:
            raise self.error


def test_posix_adapter_routes_blocking_nonblocking_and_unlock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the public adapter preserves the three flock operations used by existing call sites."""
    backend = _FakeFcntl()
    monkeypatch.setattr(file_lock, "_IS_WINDOWS", False)
    monkeypatch.setattr(file_lock, "_fcntl", backend)

    lock_file(11)
    lock_file(12, blocking=False)
    unlock_file(13)

    assert backend.calls == [
        (11, backend.LOCK_EX),
        (12, backend.LOCK_EX | backend.LOCK_NB),
        (13, backend.LOCK_UN),
    ]


def test_windows_adapter_seeds_empty_file_and_locks_byte_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Windows gets a lockable byte before using msvcrt's byte-range primitive."""
    backend = _FakeMsvcrt()
    lock_path = tmp_path / "workspace.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    monkeypatch.setattr(file_lock, "_IS_WINDOWS", True)
    monkeypatch.setattr(file_lock, "_msvcrt", backend)
    try:
        lock_file(descriptor, blocking=False)
        unlock_file(descriptor)
    finally:
        os.close(descriptor)

    assert backend.calls == [
        (descriptor, backend.LK_NBLCK, 1, 1),
        (descriptor, backend.LK_UNLCK, 1, 1),
    ]
    assert lock_path.read_bytes() == b"\0"


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EDEADLK])
def test_windows_nonblocking_contention_has_python_blocking_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    """Map Windows contention errno values to the cross-platform non-blocking exception contract."""
    backend = _FakeMsvcrt(OSError(error_number, "busy"))
    descriptor = os.open(tmp_path / "busy.lock", os.O_RDWR | os.O_CREAT, 0o600)
    monkeypatch.setattr(file_lock, "_IS_WINDOWS", True)
    monkeypatch.setattr(file_lock, "_msvcrt", backend)
    try:
        with pytest.raises(BlockingIOError) as caught:
            lock_file(descriptor, blocking=False)
    finally:
        os.close(descriptor)

    assert caught.value.errno == error_number


@pytest.mark.parametrize("state_kind", ["storage", "knowledge"])
def test_public_state_mutations_do_not_lose_updates_across_two_processes(
    tmp_path: Path,
    state_kind: str,
) -> None:
    """Characterize cross-process serialization through public storage and knowledge methods."""
    if state_kind == "storage":
        script = (
            "from mobile_auto_mcp.state.storage import LocalStore; "
            f"store=LocalStore({str(tmp_path)!r}); "
            "[store.start_session('android',[str(i)]) for i in range(10)]"
        )
    else:
        script = (
            "import os; from mobile_auto_mcp.state.knowledge import KnowledgeBase; "
            f"kb=KnowledgeBase({str(tmp_path)!r}); "
            "[kb.record_field_alias('app',str(os.getpid())+'-'+str(i),'field') for i in range(10)]"
        )
    processes = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(2)]

    assert [process.wait(timeout=10) for process in processes] == [0, 0]
    if state_kind == "storage":
        assert len(LocalStore(tmp_path).list_sessions()) == 20
    else:
        aliases = [
            KnowledgeBase(tmp_path).suggest_field_alias("app", f"{process.pid}-{index}")["field"]
            for process in processes
            for index in range(10)
        ]
        assert aliases == ["field"] * 20
