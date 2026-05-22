"""ReminderScheduler unit tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from mcgram.audit import AuditLog
from mcgram.config import Settings
from mcgram.errors import LimitError
from mcgram.reminders import ReminderScheduler


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> dict[str, Any]:
        self.sent.append(text)
        return {"message_id": 1}


def _settings(tmp_path: Path) -> Settings:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
bot:
  operator_chat_id: 99
audit:
  path: {(tmp_path / 'audit.jsonl').as_posix()}
limits:
  reminder_max_pending: 3
  reminder_max_delay_s: 100
  reminder_text_max_chars: 50
""",
        encoding="utf-8",
    )
    return Settings.load(cfg)


def _audit(p: str) -> list[dict]:
    pp = Path(p)
    if not pp.exists():
        return []
    return [
        json.loads(line)
        for line in pp.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def test_create_fires_after_delay(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    audit = AuditLog(s.audit.path)
    client = FakeClient()
    sched = ReminderScheduler(client, s, audit)
    res = sched.create("test", delay_s=1)
    assert res["reminder_id"].startswith("r_")
    # Wait for fire
    for _ in range(20):
        if client.sent:
            break
        await asyncio.sleep(0.1)
    assert client.sent == ["⏰ test"]
    assert any(r["status"] == "ok" for r in _audit(s.audit.path) if r["tool"] == "_reminder_fire")


async def test_cancel_before_fire(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    audit = AuditLog(s.audit.path)
    client = FakeClient()
    sched = ReminderScheduler(client, s, audit)
    res = sched.create("nope", delay_s=10)
    rid = res["reminder_id"]
    assert sched.cancel(rid) is True
    # Drive event loop briefly so the cancellation propagates
    await asyncio.sleep(0.05)
    assert client.sent == []
    assert sched.cancel(rid) is False  # already cancelled


async def test_list_pending(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    sched = ReminderScheduler(FakeClient(), s, AuditLog(s.audit.path))
    sched.create("a", delay_s=10)
    sched.create("b", delay_s=20)
    items = sched.list()
    assert len(items) == 2
    assert {it["text"] for it in items} == {"a", "b"}


async def test_max_pending_enforced(tmp_path: Path) -> None:
    s = _settings(tmp_path)  # max_pending = 3
    sched = ReminderScheduler(FakeClient(), s, AuditLog(s.audit.path))
    sched.create("a", delay_s=10)
    sched.create("b", delay_s=10)
    sched.create("c", delay_s=10)
    with pytest.raises(LimitError) as exc:
        sched.create("d", delay_s=10)
    assert exc.value.kind == "reminder_max_pending"


async def test_delay_capped(tmp_path: Path) -> None:
    s = _settings(tmp_path)  # max_delay_s = 100
    sched = ReminderScheduler(FakeClient(), s, AuditLog(s.audit.path))
    with pytest.raises(LimitError) as exc:
        sched.create("x", delay_s=999)
    assert exc.value.kind == "reminder_max_delay_s"


async def test_text_required(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    sched = ReminderScheduler(FakeClient(), s, AuditLog(s.audit.path))
    with pytest.raises(LimitError):
        sched.create("", delay_s=1)


async def test_text_too_long(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    sched = ReminderScheduler(FakeClient(), s, AuditLog(s.audit.path))
    with pytest.raises(LimitError) as exc:
        sched.create("x" * 100, delay_s=1)
    assert exc.value.kind == "reminder_text_too_long"


async def test_shutdown_cancels_all(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    sched = ReminderScheduler(FakeClient(), s, AuditLog(s.audit.path))
    sched.create("a", delay_s=10)
    sched.create("b", delay_s=10)
    sched.shutdown()
    await asyncio.sleep(0.05)
    assert sched.list() == []
