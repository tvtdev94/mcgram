"""AuditLog unit tests."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from mcgram.audit import AuditLog


def _read_jsonl(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_write_basic(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p)
    a.write({"tool": "send_message", "status": "ok", "chat_id": 1})
    records = _read_jsonl(p)
    assert len(records) == 1
    r = records[0]
    assert r["tool"] == "send_message"
    assert r["status"] == "ok"
    assert r["chat_id"] == 1
    assert "ts" in r  # auto-added


def test_write_preserves_explicit_ts(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p)
    a.write({"ts": "2026-01-01T00:00:00+00:00", "tool": "x", "status": "ok"})
    assert _read_jsonl(p)[0]["ts"] == "2026-01-01T00:00:00+00:00"


def test_redact_text_replaces_and_keeps_length(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p, redact_text=True)
    a.write({"tool": "send_message", "status": "ok", "text": "hello world"})
    r = _read_jsonl(p)[0]
    assert r["text"] == "<redacted>"
    assert r["text_len"] == len("hello world")


def test_redact_text_off_keeps_text(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p, redact_text=False)
    a.write({"tool": "send_message", "status": "ok", "text": "hello"})
    r = _read_jsonl(p)[0]
    assert r["text"] == "hello"
    assert "text_len" not in r


def test_rotation(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p, rotate_mb=1)
    a.rotate_bytes = 200  # force rotation cheaply
    for i in range(20):
        a.write({"tool": "t", "status": "ok", "i": i, "pad": "x" * 30})
    names = sorted(f.name for f in tmp_path.iterdir())
    assert "audit.jsonl" in names
    assert "audit.jsonl.1" in names


def test_rotation_chain_drops_oldest(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p)
    a.rotate_bytes = 100
    for _ in range(40):
        a.write({"tool": "t", "status": "ok", "pad": "x" * 50})
    # After many rotations only .1 .. .3 should remain
    names = sorted(f.name for f in tmp_path.iterdir() if f.name.startswith("audit.jsonl"))
    extras = [n for n in names if n.startswith("audit.jsonl.")]
    nums = [int(n.rsplit(".", 1)[-1]) for n in extras]
    assert all(1 <= n <= 3 for n in nums)


def test_retention_prunes_old_entries(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    old = (datetime.now().astimezone() - timedelta(days=10)).isoformat(timespec="seconds")
    new = datetime.now().astimezone().isoformat(timespec="seconds")
    p.write_text(
        json.dumps({"ts": old, "tool": "t", "status": "ok", "n": 1}) + "\n"
        + json.dumps({"ts": new, "tool": "t", "status": "ok", "n": 2}) + "\n",
        encoding="utf-8",
    )
    AuditLog(p, retention_days=7)  # ctor runs prune
    records = _read_jsonl(p)
    assert len(records) == 1
    assert records[0]["n"] == 2


def test_retention_keeps_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    p.write_text("not valid json\n", encoding="utf-8")
    AuditLog(p, retention_days=7)
    # Malformed line preserved (fail-safe)
    assert "not valid json" in p.read_text()


def test_creates_parent_dir(tmp_path: Path) -> None:
    p = tmp_path / "deep" / "nested" / "audit.jsonl"
    AuditLog(p).write({"tool": "t", "status": "ok"})
    assert p.exists()


def test_fsync_path(tmp_path: Path, monkeypatch) -> None:
    """Verify fsync is invoked per write."""
    called: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (called.append(fd), real_fsync(fd))[1])
    p = tmp_path / "audit.jsonl"
    AuditLog(p).write({"tool": "t", "status": "ok"})
    assert called  # at least one fsync


# --- concurrent writers ------------------------------------------------------
# Several mcgram processes share one audit file (one per Claude Code session).


def test_concurrent_writers_do_not_lose_lines(tmp_path: Path) -> None:
    """Two AuditLog instances on one path must both append, never overwrite."""
    p = tmp_path / "audit.jsonl"
    a, b = AuditLog(p), AuditLog(p)
    for i in range(25):
        a.write({"tool": "a", "status": "ok", "n": i})
        b.write({"tool": "b", "status": "ok", "n": i})
    records = _read_jsonl(p)  # parses → no torn/interleaved lines
    assert len(records) == 50
    assert sum(r["tool"] == "a" for r in records) == 25
    assert sum(r["tool"] == "b" for r in records) == 25


def test_rotation_lock_prevents_concurrent_rotate(tmp_path: Path) -> None:
    """A second rotater backs off instead of racing the rename chain.

    Without the lock, both processes shuffle `.jsonl -> .1 -> .2 -> .3` at once
    and a backup is lost.
    """
    from mcgram.audit import _CrossProcessLock

    p = tmp_path / "audit.jsonl"
    a = AuditLog(p, rotate_mb=1)
    a.write({"tool": "t", "status": "ok", "pad": "x" * 2_000_000})

    rotated: list[bool] = []
    b = AuditLog(p, rotate_mb=1)
    b._rotate_backups = lambda: rotated.append(True)  # type: ignore[method-assign]

    # Hold the lock as if another process were mid-rotation.
    with _CrossProcessLock(f"{p}.rotate.lock") as acquired:
        assert acquired
        b._maybe_rotate()
    assert rotated == []  # skipped, not raced

    # Lock free again → rotation proceeds.
    b._maybe_rotate()
    assert rotated == [True]


def test_stale_rotate_lock_is_reclaimed(tmp_path: Path) -> None:
    """A lock left by a killed process must not disable rotation forever."""
    from mcgram.audit import _ROTATE_LOCK_STALE_S, _CrossProcessLock

    lock_path = tmp_path / "audit.jsonl.rotate.lock"
    lock_path.write_text("", encoding="utf-8")
    stale = time.time() - (_ROTATE_LOCK_STALE_S + 10)
    os.utime(lock_path, (stale, stale))

    with _CrossProcessLock(str(lock_path)) as acquired:
        assert acquired is True
    assert not lock_path.exists()  # released on exit


def test_fresh_rotate_lock_is_respected(tmp_path: Path) -> None:
    lock_path = tmp_path / "audit.jsonl.rotate.lock"
    from mcgram.audit import _CrossProcessLock

    with _CrossProcessLock(str(lock_path)) as first:
        assert first is True
        with _CrossProcessLock(str(lock_path)) as second:
            assert second is False
