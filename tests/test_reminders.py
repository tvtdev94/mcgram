"""ReminderScheduler unit tests."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from mcgram.audit import AuditLog
from mcgram.broker_wiring import build_reminder_store
from mcgram.config import Settings
from mcgram.errors import LimitError
from mcgram.rate_limiter import RateLimiter
from mcgram.reminders import ReminderScheduler
from mcgram.runtime import AppState
from mcgram.spool import Spool
from mcgram.update_dispatcher import UpdateDispatcher
from tests.test_reminder_spool import publish_from_dead_process


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> dict[str, Any]:
        self.sent.append(text)
        return {"message_id": 1}


def _scheduler(
    s: Settings, client: Any, audit: AuditLog | None = None, *, durable: bool = False
) -> ReminderScheduler:
    """Build a scheduler over a minimal AppState — delivery routes via dispatch.

    `durable` adds the production disk store (as `server._wire_phase3` does)."""
    state = AppState(
        settings=s,
        dispatcher=UpdateDispatcher(),
        rate=RateLimiter(s.defaults.rate_limit_per_min),
        audit=audit or AuditLog(s.audit.path),
        client=client,
    )
    return ReminderScheduler(state, store=build_reminder_store(s) if durable else None)


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
    sched = _scheduler(s, client, audit)
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
    sched = _scheduler(s, client, audit)
    res = sched.create("nope", delay_s=10)
    rid = res["reminder_id"]
    assert sched.cancel(rid) is True
    # Drive event loop briefly so the cancellation propagates
    await asyncio.sleep(0.05)
    assert client.sent == []
    assert sched.cancel(rid) is False  # already cancelled


async def test_list_pending(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    sched = _scheduler(s, FakeClient())
    sched.create("a", delay_s=10)
    sched.create("b", delay_s=20)
    items = sched.list()
    assert len(items) == 2
    assert {it["text"] for it in items} == {"a", "b"}


async def test_max_pending_enforced(tmp_path: Path) -> None:
    s = _settings(tmp_path)  # max_pending = 3
    sched = _scheduler(s, FakeClient())
    sched.create("a", delay_s=10)
    sched.create("b", delay_s=10)
    sched.create("c", delay_s=10)
    with pytest.raises(LimitError) as exc:
        sched.create("d", delay_s=10)
    assert exc.value.kind == "reminder_max_pending"


async def test_delay_capped(tmp_path: Path) -> None:
    s = _settings(tmp_path)  # max_delay_s = 100
    sched = _scheduler(s, FakeClient())
    with pytest.raises(LimitError) as exc:
        sched.create("x", delay_s=999)
    assert exc.value.kind == "reminder_max_delay_s"


async def test_text_required(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    sched = _scheduler(s, FakeClient())
    with pytest.raises(LimitError):
        sched.create("", delay_s=1)


async def test_text_too_long(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    sched = _scheduler(s, FakeClient())
    with pytest.raises(LimitError) as exc:
        sched.create("x" * 100, delay_s=1)
    assert exc.value.kind == "reminder_text_too_long"


async def test_shutdown_cancels_all(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    sched = _scheduler(s, FakeClient())
    sched.create("a", delay_s=10)
    sched.create("b", delay_s=10)
    sched.shutdown()
    await asyncio.sleep(0.05)
    assert sched.list() == []


# --- durability: reminders survive a restart ---------------------------------


async def _until(predicate: Any, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _on_disk(s: Settings) -> set[str]:
    return Spool(s.spool_root, "reminders").keys("rec")


def test_constructing_a_durable_scheduler_needs_no_event_loop(tmp_path: Path) -> None:
    sched = _scheduler(_settings(tmp_path), FakeClient(), durable=True)
    assert sched.store is not None  # built outside any running loop without error


async def test_saved_on_create_removed_on_fire_and_on_cancel(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    client = FakeClient()
    sched = _scheduler(s, client, durable=True)
    fired = sched.create("soon", delay_s=1)["reminder_id"]
    cancelled = sched.create("never", delay_s=50)["reminder_id"]
    assert _on_disk(s) == {fired, cancelled}
    assert sched.cancel(cancelled)
    assert _on_disk(s) == {fired}
    assert await _until(lambda: client.sent == ["⏰ soon"])
    assert await _until(lambda: _on_disk(s) == set())


async def test_shutdown_keeps_saved_reminders_for_the_next_process(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    sched = _scheduler(s, FakeClient(), durable=True)
    rid = sched.create("later", delay_s=50)["reminder_id"]
    sched.shutdown()
    await asyncio.sleep(0.05)
    assert _on_disk(s) == {rid}


async def test_restart_fires_overdue_reminders_once_marked_late(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    now = time.time()
    publish_from_dead_process(s.spool_root, ("r_0000a001", now - 30), ("r_0000a002", now + 50))
    client = FakeClient()
    sched = _scheduler(s, client, durable=True)
    sched.start()
    try:
        assert await _until(lambda: len(client.sent) == 1)
        assert client.sent[0].startswith("⏰ from r_0000a001\n\n(late: was due ")
        # Not yet due: adopted and pending, not fired early.
        assert [r["id"] for r in sched.list()] == ["r_0000a002"]
        assert await _until(lambda: _on_disk(s) == {"r_0000a002"})
        await asyncio.sleep(0.1)
        assert len(client.sent) == 1  # fired exactly once
    finally:
        sched.shutdown()
    adopted = [r for r in _audit(s.audit.path) if r["tool"] == "_reminder_adopt"]
    assert {r["rid"]: r["late"] for r in adopted} == {"r_0000a001": True, "r_0000a002": False}


async def test_restart_respects_the_pending_cap(tmp_path: Path) -> None:
    s = _settings(tmp_path)  # max_pending = 3
    later = time.time() + 50
    publish_from_dead_process(s.spool_root, *((f"r_0000b00{i}", later) for i in range(5)))
    sched = _scheduler(s, FakeClient(), durable=True)
    sched.start()
    try:
        assert await _until(lambda: len(sched.list()) == 3)
        await asyncio.sleep(0.1)
        assert len(sched.list()) == 3
    finally:
        sched.shutdown()
    assert len(_on_disk(s)) == 5  # the rest wait on disk for capacity


async def test_adopted_reminders_do_not_use_up_this_sessions_own_cap(tmp_path: Path) -> None:
    s = _settings(tmp_path)  # max_pending = 3
    later = time.time() + 50
    publish_from_dead_process(s.spool_root, *((f"r_0000c00{i}", later) for i in range(3)))
    sched = _scheduler(s, FakeClient(), durable=True)
    sched.start()
    try:
        assert await _until(lambda: len(sched.list()) == 3)
        for text in ("a", "b", "c"):
            sched.create(text, delay_s=50)
        with pytest.raises(LimitError) as exc:
            sched.create("d", delay_s=50)
        assert exc.value.kind == "reminder_max_pending"
        assert len(sched.list()) == 6
    finally:
        sched.shutdown()


async def test_cancel_request_from_another_session_is_honoured(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    s.broker.enabled = True
    s.broker.poll_interval_s = 0.02
    client = FakeClient()
    sched = _scheduler(s, client, durable=True)
    sched.start()
    try:
        rid = sched.create("stop me", delay_s=50)["reminder_id"]
        other_session = build_reminder_store(s)
        assert other_session is not None
        assert other_session.request_cancel(rid) is not None
        assert await _until(lambda: sched.list() == [])
    finally:
        sched.shutdown()
    assert _on_disk(s) == set()
    assert client.sent == []


async def test_heartbeat_is_written_on_its_own_cadence_only_while_owning_reminders(
    tmp_path: Path,
) -> None:
    s = _settings(tmp_path)
    s.broker.enabled, s.broker.poll_interval_s = True, 0.02  # fast watcher ticks
    sched = _scheduler(s, FakeClient(), durable=True)
    heartbeats = Spool(s.spool_root, "reminders")
    sched.start()
    try:
        await asyncio.sleep(0.2)
        assert heartbeats.keys("alive") == set()  # nothing owned, nothing to prove
        sched.create("later", delay_s=50)
        await asyncio.sleep(0.1)  # the watcher takes over beating on its next tick
        [owner] = heartbeats.keys("alive")
        first = heartbeats.read(owner, "alive")
        await asyncio.sleep(0.3)  # many ticks, far less than one beat interval
        assert heartbeats.read(owner, "alive") == first
    finally:
        sched.shutdown()


async def test_watch_loop_survives_a_failing_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _settings(tmp_path)
    s.broker.enabled = True
    s.broker.poll_interval_s = 0.02
    sched = _scheduler(s, FakeClient(), durable=True)
    assert sched.store is not None
    real = sched.store.finished
    calls = {"n": 0}

    def flaky(rids: list[str]) -> list[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk hiccup")
        return real(rids)

    monkeypatch.setattr(sched.store, "finished", flaky)
    sched.start()
    try:
        rid = sched.create("stop me", delay_s=50)["reminder_id"]
        assert sched.store.request_cancel(rid) is not None
        assert await _until(lambda: sched.list() == [])
    finally:
        sched.shutdown()
    assert calls["n"] > 1


async def test_fired_reminder_whose_record_survives_is_never_fired_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows a concurrent reader makes the record's unlink fail (WinError
    32), so the record outlives the reminder. Another session that later takes
    it for an orphan must not send it again; the owner keeps retrying the
    delete instead."""
    s = _settings(tmp_path)
    s.broker.enabled, s.broker.poll_interval_s = True, 0.02  # fast watcher ticks
    publish_from_dead_process(s.spool_root, ("r_0000d001", time.time() - 1))
    real_unlink = Path.unlink
    busy = {"rec": True}

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if busy["rec"] and self.name.endswith(".rec.json"):
            raise PermissionError(32, "being used by another process", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    client = FakeClient()
    owner = _scheduler(s, client, durable=True)
    other = _scheduler(s, client, durable=True)
    assert other.store is not None
    other.store._instance = "0123456789abcdef"  # a different session's token
    owner.start()
    try:
        assert await _until(lambda: len(client.sent) == 1)
        assert _on_disk(s) == {"r_0000d001"}  # the delete really failed
        other.start()
        assert not await _until(lambda: len(client.sent) > 1, timeout=0.3)
        busy["rec"] = False
        assert await _until(lambda: _on_disk(s) == set())
    finally:
        other.shutdown()
        owner.shutdown()
