"""SingleInstanceLock unit tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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
