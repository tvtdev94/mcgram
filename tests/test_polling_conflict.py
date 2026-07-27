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


async def test_409_backs_off_instead_of_spinning(tmp_path: Path) -> None:
    """Two pollers on one token (MCGRAM_SKIP_LOCK=1) must not hot-loop.

    With the lock bypassed, the losing poller gets 409 on every attempt. It has
    to sleep between them — a tight retry loop would hammer Telegram and spam
    the audit log.
    """
    import mcgram.polling as polling_module

    audit = AuditLog(tmp_path / "audit.jsonl")
    conflict = TelegramError(409, "Conflict: terminated by other getUpdates request")
    client = FakeClient([conflict] * 50)

    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def spy_sleep(delay: float) -> None:
        slept.append(delay)
        await real_sleep(0)  # keep the loop moving without real waiting

    monkey = pytest.MonkeyPatch()
    monkey.setattr(polling_module.asyncio, "sleep", spy_sleep)
    try:
        task = asyncio.create_task(
            poll_loop(
                client, UpdateDispatcher(), 1, audit,  # type: ignore[arg-type]
                long_poll_timeout_s=1, initial_backoff_s=0.01,
            )
        )
        for _ in range(50):
            if len(slept) >= 5:
                break
            await real_sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        monkey.undo()

    assert slept, "409 must back off, not retry immediately"
    # Every 409 sleeps for the dedicated conflict backoff, not the 1s generic one.
    assert all(d == polling_module._CONFLICT_BACKOFF_S for d in slept[:5])


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
