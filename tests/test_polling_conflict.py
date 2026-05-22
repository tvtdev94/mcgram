"""poll_loop: Telegram 409 Conflict handling."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mcgram.audit import AuditLog
from mcgram.errors import TelegramError
from mcgram.polling import poll_loop
from mcgram.update_dispatcher import UpdateDispatcher


class FakeClient:
    def __init__(self, sequence: list) -> None:
        self._seq = list(sequence)

    async def get_updates(self, *, offset: int = 0, timeout: int = 25) -> list:
        if not self._seq:
            await asyncio.sleep(0.05)
            return []
        nxt = self._seq.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _audit_records(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def test_409_recorded_as_conflict(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    client = FakeClient([
        TelegramError(409, "Conflict: terminated by other getUpdates request"),
        [],
    ])
    task = asyncio.create_task(
        poll_loop(
            client, UpdateDispatcher(), 1, audit,  # type: ignore[arg-type]
            long_poll_timeout_s=1, initial_backoff_s=0.01,
        )
    )
    for _ in range(30):
        recs = _audit_records(tmp_path / "audit.jsonl")
        if any(r.get("status") == "conflict" for r in recs):
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    conflicts = [r for r in _audit_records(tmp_path / "audit.jsonl")
                 if r.get("status") == "conflict"]
    assert conflicts, "expected at least one 409 conflict audit row"
    assert conflicts[0]["reason"] == "409_another_poller"


async def test_other_telegram_error_treated_as_transient(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    client = FakeClient([TelegramError(500, "internal"), []])
    task = asyncio.create_task(
        poll_loop(
            client, UpdateDispatcher(), 1, audit,  # type: ignore[arg-type]
            long_poll_timeout_s=1, initial_backoff_s=0.01,
        )
    )
    for _ in range(30):
        recs = _audit_records(tmp_path / "audit.jsonl")
        if any(r.get("status") == "error" for r in recs):
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    errors = [r for r in _audit_records(tmp_path / "audit.jsonl")
              if r.get("status") == "error"]
    assert errors
