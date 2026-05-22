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
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

    def _maybe_rotate(self) -> None:
        try:
            size = os.path.getsize(self.path)
        except FileNotFoundError:
            return
        if size < self.rotate_bytes:
            return
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
        for path in (self.path, f"{self.path}.1", f"{self.path}.2", f"{self.path}.3"):
            if os.path.exists(path):
                _rewrite_newer_than(path, cutoff)
        self._last_prune_ts = time.monotonic()


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
