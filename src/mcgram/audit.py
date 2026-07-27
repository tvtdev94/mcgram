"""JSONL audit logger with fsync, rotation, retention, and optional text redaction."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("mcgram.audit")

_PRUNE_INTERVAL_S = 3600.0


def _resolve_tz(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        log.warning("unknown timezone %r; falling back to UTC", name)
        try:
            return ZoneInfo("UTC")
        except ZoneInfoNotFoundError:
            return UTC


class AuditLog:
    """Thread-safe append-only JSONL writer.

    Each record is one JSON line with `ts`, `tool`, `status` keys at minimum.
    `fsync` is called per-write so a `kill -9` still preserves the audit trail.

    Several mcgram processes share one audit file (one per Claude Code session).
    Appends are opened `O_APPEND`, so each write lands at the true end of file
    and short lines don't interleave. Rotation and pruning rewrite the file, so
    they take a cross-process lock — without it, two processes rotating at once
    race on the `.jsonl -> .1 -> .2 -> .3` renames and lose a backup.
    """

    def __init__(
        self,
        path: str | Path,
        rotate_mb: int = 25,
        timezone: str = "UTC",
        redact_text: bool = False,
        retention_days: int | None = None,
    ) -> None:
        self.path = str(Path(path).expanduser())
        self.rotate_bytes = max(1, rotate_mb) * 1024 * 1024
        self.redact_text = redact_text
        self.retention_days = retention_days
        self._tz = _resolve_tz(timezone)
        self._lock = threading.Lock()
        self._last_prune_ts = 0.0
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if retention_days:
            with self._lock:
                self._prune_old_entries()

    def write(self, record: dict[str, Any]) -> None:
        """Append a record. `ts` is set automatically if missing."""
        rec = dict(record)
        rec.setdefault("ts", datetime.now(self._tz).isoformat(timespec="seconds"))
        if self.redact_text and "text" in rec:
            text = rec["text"]
            if isinstance(text, str):
                rec["text_len"] = len(text)
                rec["text"] = "<redacted>"
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            self._maybe_rotate()
            self._maybe_prune()
            self._append(line)

    def _append(self, line: str) -> None:
        """Append one line with O_APPEND so concurrent writers can't overwrite.

        Plain `open(..., "a")` is already O_APPEND on CPython, but stating the
        flag makes the cross-process contract explicit: the kernel resolves the
        write offset at write time, so a line written by another process between
        our open and our write is never clobbered.
        """
        data = line.encode("utf-8")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _maybe_rotate(self) -> None:
        try:
            size = os.path.getsize(self.path)
        except FileNotFoundError:
            return
        if size < self.rotate_bytes:
            return
        with _CrossProcessLock(f"{self.path}.rotate.lock") as acquired:
            if not acquired:
                return  # another process is rotating right now — let it.
            # Re-check under the lock: the winner may have already rotated,
            # in which case the file is now small and we'd rotate a fresh log.
            try:
                if os.path.getsize(self.path) < self.rotate_bytes:
                    return
            except FileNotFoundError:
                return
            self._rotate_backups()

    def _rotate_backups(self) -> None:
        tail = f"{self.path}.3"
        if os.path.exists(tail):
            try:
                os.remove(tail)
            except OSError as e:
                log.warning("rotate: failed to remove %s: %s", tail, e)
                return
        for i in (3, 2, 1):
            src = self.path if i == 1 else f"{self.path}.{i - 1}"
            dst = f"{self.path}.{i}"
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError as e:
                    log.warning("rotate: %s -> %s failed: %s", src, dst, e)
                    return

    def _maybe_prune(self) -> None:
        if not self.retention_days:
            return
        now = time.monotonic()
        if now - self._last_prune_ts < _PRUNE_INTERVAL_S:
            return
        self._prune_old_entries()

    def _prune_old_entries(self) -> None:
        if not self.retention_days:
            return
        cutoff = datetime.now(self._tz) - timedelta(days=self.retention_days)
        # Same lock as rotation: pruning rewrites the live file, so it must not
        # overlap with another process shuffling the same paths.
        with _CrossProcessLock(f"{self.path}.rotate.lock") as acquired:
            if not acquired:
                # Someone else is pruning. Record the attempt so we back off for
                # the full interval instead of retrying on every write.
                self._last_prune_ts = time.monotonic()
                return
            for path in (self.path, f"{self.path}.1", f"{self.path}.2", f"{self.path}.3"):
                if os.path.exists(path):
                    _rewrite_newer_than(path, cutoff)
        self._last_prune_ts = time.monotonic()


#: A rotate lock older than this is assumed abandoned by a killed process.
_ROTATE_LOCK_STALE_S = 60.0


class _CrossProcessLock:
    """Best-effort exclusive lock via `O_CREAT | O_EXCL`, portable to Windows.

    Non-blocking by design: `__enter__` returns whether the lock was taken, and
    the caller skips the work if it wasn't. Audit rotation is idempotent enough
    that losing the race means "someone else already did it", not "data lost".

    A lock file left behind by a killed process is reclaimed after
    `_ROTATE_LOCK_STALE_S`, so one crash can't disable rotation permanently.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> bool:
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if not self._reclaim_if_stale():
                return False
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except OSError:
                return False
        except OSError as e:
            log.debug("rotate lock: open %s failed: %s", self.path, e)
            return False
        return True

    def _reclaim_if_stale(self) -> bool:
        try:
            age = time.time() - os.path.getmtime(self.path)
        except OSError:
            return False
        if age < _ROTATE_LOCK_STALE_S:
            return False
        log.warning("rotate lock %s is %.0fs old — reclaiming.", self.path, age)
        try:
            os.remove(self.path)
        except OSError:
            return False
        return True

    def __exit__(self, *exc: object) -> None:
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            os.close(self._fd)
        with contextlib.suppress(OSError):
            os.remove(self.path)
        self._fd = None


def _rewrite_newer_than(path: str, cutoff: datetime) -> None:
    tmp = f"{path}.tmp"
    try:
        with open(path, encoding="utf-8") as src:
            kept = [line for line in src if _entry_newer_than(line, cutoff)]
        with open(tmp, "w", encoding="utf-8") as dst:
            dst.writelines(kept)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, path)
    except OSError as e:
        log.warning("prune: rewrite %s failed: %s", path, e)
        with contextlib.suppress(OSError):
            os.remove(tmp)


def _entry_newer_than(line: str, cutoff: datetime) -> bool:
    """Keep entries with `ts >= cutoff`. Malformed lines kept (fail-safe)."""
    try:
        rec = json.loads(line)
        ts = datetime.fromisoformat(rec["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=cutoff.tzinfo)
        return ts >= cutoff
    except (json.JSONDecodeError, ValueError, KeyError, TypeError):
        return True
