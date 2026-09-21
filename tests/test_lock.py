"""SingleInstanceLock unit tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import mcgram.lock as lock_mod
from mcgram.errors import LockHeldError
from mcgram.lock import SingleInstanceLock


def test_acquire_writes_pid(tmp_path: Path) -> None:
    p = tmp_path / ".lock"
    with SingleInstanceLock(p):
        assert p.exists()
        assert int(p.read_text()) == os.getpid()
    assert not p.exists()


def test_release_idempotent(tmp_path: Path) -> None:
    p = tmp_path / ".lock"
    lock = SingleInstanceLock(p)
    lock.release()  # never acquired — must not raise


def test_stale_lock_auto_cleanup(tmp_path: Path) -> None:
    p = tmp_path / ".lock"
    p.write_text("999999")  # almost certainly dead pid
    with SingleInstanceLock(p):
        assert int(p.read_text()) == os.getpid()


def test_corrupt_lock_treated_as_stale(tmp_path: Path) -> None:
    p = tmp_path / ".lock"
    p.write_text("not-a-pid")
    with SingleInstanceLock(p):
        assert int(p.read_text()) == os.getpid()


def test_held_lock_raises(tmp_path: Path) -> None:
    p = tmp_path / ".lock"
    p.write_text(str(os.getpid()))  # current process is alive
    try:
        with pytest.raises(LockHeldError) as exc:
            SingleInstanceLock(p).acquire()
        assert exc.value.pid == os.getpid()
    finally:
        p.unlink(missing_ok=True)


def test_creates_parent_dir(tmp_path: Path) -> None:
    p = tmp_path / "deep" / ".lock"
    with SingleInstanceLock(p):
        assert p.exists()
    assert not p.exists()


def test_pending_pid_write_is_respected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock file caught empty belongs to a live owner mid-write, not to nobody.

    `acquire` creates with O_EXCL and writes the pid as a second syscall. A
    reader that sees the gap and unlinks hands the lock to two processes.
    """
    path = tmp_path / ".lock"
    path.write_text("", encoding="utf-8")
    seen = {"n": 0}

    def _read_pid_late(_p: Path) -> int | None:
        seen["n"] += 1
        return None if seen["n"] == 1 else os.getpid()

    monkeypatch.setattr(lock_mod, "_read_pid", _read_pid_late)
    with pytest.raises(LockHeldError):
        SingleInstanceLock(path).acquire()
    assert path.exists(), "a live owner's lock file must survive"


def test_stale_empty_lock_is_still_reclaimed(tmp_path: Path) -> None:
    """The grace period must not brick polling after a crash mid-write."""
    path = tmp_path / ".lock"
    path.write_text("", encoding="utf-8")
    lock = SingleInstanceLock(path)
    assert lock.acquire() == os.getpid()
    assert int(path.read_text(encoding="utf-8")) == os.getpid()
    lock.release()


def test_unlink_refused_means_lock_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows refuses to unlink a file another process holds open."""
    path = tmp_path / ".lock"
    path.write_text("", encoding="utf-8")

    def _refuse(self: Path, **kwargs: object) -> None:
        raise PermissionError(32, "in use by another process")

    monkeypatch.setattr(Path, "unlink", _refuse)
    with pytest.raises(LockHeldError):
        SingleInstanceLock(path).acquire()
