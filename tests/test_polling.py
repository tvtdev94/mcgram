"""Polling loop tests using a hand-crafted FakeClient (avoids httpx_mock spin)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from mcgram.audit import AuditLog
from mcgram.polling import poll_loop
from mcgram.update_dispatcher import UpdateDispatcher


def _audit(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


class FakeClient:
    """Minimal stand-in implementing only `get_updates` to drive poll_loop."""

    def __init__(self, batches: list[list[dict[str, Any]] | Exception]) -> None:
        self._batches = list(batches)
        self.calls = 0

    async def get_updates(self, *, offset: int = 0, timeout: int = 25) -> list[dict[str, Any]]:
        self.calls += 1
        if not self._batches:
            await asyncio.sleep(0.05)
            return []
        nxt = self._batches.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


async def test_poll_loop_dispatches_operator_updates(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    async def capture(upd: dict[str, Any]) -> None:
        seen.append(upd)

    dispatcher = UpdateDispatcher()
    dispatcher.register(capture)
    audit = AuditLog(tmp_path / "audit.jsonl")
    client = FakeClient([
        [
            {"update_id": 1, "message": {"chat": {"id": 42}, "text": "yo"}},
            {"update_id": 2, "message": {"chat": {"id": 999}, "text": "spam"}},
        ],
    ])

    task = asyncio.create_task(
        poll_loop(
            client, dispatcher, 42, audit,  # type: ignore[arg-type]
            long_poll_timeout_s=1, initial_backoff_s=0.01,
        )
    )
    for _ in range(40):
        if seen:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(seen) == 1
    assert seen[0]["update_id"] == 1
    rejected = [r for r in _audit(tmp_path / "audit.jsonl") if r["status"] == "rejected"]
    assert any(r["reason"] == "non_operator" for r in rejected)


async def test_poll_loop_recovers_from_error(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    client = FakeClient([RuntimeError("transient"), []])
    task = asyncio.create_task(
        poll_loop(
            client, UpdateDispatcher(), 1, audit,  # type: ignore[arg-type]
            long_poll_timeout_s=1, initial_backoff_s=0.01,
        )
    )
    for _ in range(30):
        recs = _audit(tmp_path / "audit.jsonl")
        if any(r["status"] == "error" for r in recs):
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    errs = [r for r in _audit(tmp_path / "audit.jsonl") if r["status"] == "error"]
    assert errs


async def test_poll_loop_offset_advances(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    seen: list[int] = []

    async def cap(upd: dict[str, Any]) -> None:
        seen.append(upd["update_id"])

    dispatcher = UpdateDispatcher()
    dispatcher.register(cap)
    client = FakeClient([
        [{"update_id": 5, "message": {"chat": {"id": 1}, "text": "a"}}],
        [{"update_id": 6, "message": {"chat": {"id": 1}, "text": "b"}}],
    ])
    task = asyncio.create_task(
        poll_loop(
            client, dispatcher, 1, audit,  # type: ignore[arg-type]
            long_poll_timeout_s=1, initial_backoff_s=0.01,
        )
    )
    for _ in range(40):
        if len(seen) >= 2:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == [5, 6]
