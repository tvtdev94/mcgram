"""Reminder tool handlers (set/cancel/list)."""

from __future__ import annotations

import time

from mcgram.broker_wiring import build_reminder_store
from mcgram.reminders import ReminderScheduler
from mcgram.runtime import AppState
from mcgram.tools import cancel_reminder, list_reminders, set_reminder
from tests.test_reminder_spool import publish_from_dead_process


def _wire(app_state: AppState) -> ReminderScheduler:
    sched = ReminderScheduler(app_state)
    app_state.reminders = sched
    return sched


async def test_set_then_list_then_cancel(app_state: AppState) -> None:
    _wire(app_state)
    created = await set_reminder.handle(app_state, text="hi", delay_s=10)
    assert created["reminder_id"].startswith("r_")
    listed = await list_reminders.handle(app_state)
    assert len(listed["reminders"]) == 1
    cancelled = await cancel_reminder.handle(app_state, reminder_id=created["reminder_id"])
    assert cancelled == {"ok": True}
    listed2 = await list_reminders.handle(app_state)
    assert listed2["reminders"] == []


async def test_set_rejected_when_limit_exceeded(app_state: AppState) -> None:
    sched = _wire(app_state)
    app_state.settings.limits.reminder_max_pending = 1
    await set_reminder.handle(app_state, text="a", delay_s=10)
    out = await set_reminder.handle(app_state, text="b", delay_s=10)
    assert out["error"] == "rejected"
    assert out["reason"] == "reminder_max_pending"
    sched.shutdown()


async def test_cancel_unknown_id(app_state: AppState) -> None:
    _wire(app_state)
    out = await cancel_reminder.handle(app_state, reminder_id="r_none")
    assert out == {"ok": False}


def _wire_durable(app_state: AppState, *, broker: bool) -> ReminderScheduler:
    app_state.settings.broker.enabled = broker
    sched = ReminderScheduler(app_state, store=build_reminder_store(app_state.settings))
    app_state.reminders = sched
    return sched


async def test_broker_lists_reminders_of_other_sessions(app_state: AppState) -> None:
    sched = _wire_durable(app_state, broker=True)
    publish_from_dead_process(app_state.settings.spool_root, ("r_0000c001", time.time() + 60))
    mine = await set_reminder.handle(app_state, text="mine", delay_s=60)
    listed = {r["id"]: r for r in (await list_reminders.handle(app_state))["reminders"]}
    assert listed[mine["reminder_id"]]["local"] is True
    assert listed[mine["reminder_id"]]["owner_alive"] is True
    other = listed["r_0000c001"]
    assert (other["local"], other["owner_alive"]) == (False, False)
    assert other["text"] == "from r_0000c001"
    assert isinstance(other["owner_pid"], int)
    sched.shutdown()


async def test_without_broker_list_shows_only_this_session_unchanged(app_state: AppState) -> None:
    sched = _wire_durable(app_state, broker=False)
    publish_from_dead_process(app_state.settings.spool_root, ("r_0000c002", time.time() + 60))
    await set_reminder.handle(app_state, text="mine", delay_s=60)
    [item] = (await list_reminders.handle(app_state))["reminders"]
    assert set(item) == {"id", "text", "fires_at", "channel", "transport"}
    out = await cancel_reminder.handle(app_state, reminder_id="r_0000c002")
    assert out == {"ok": False}  # no cross-session cancel without the broker
    sched.shutdown()


async def test_broker_cancel_of_another_sessions_reminder_is_a_request(
    app_state: AppState,
) -> None:
    sched = _wire_durable(app_state, broker=True)
    publish_from_dead_process(app_state.settings.spool_root, ("r_0000c003", time.time() + 60))
    out = await cancel_reminder.handle(app_state, reminder_id="r_0000c003")
    assert out["ok"] is True and out["via"] == "broker"
    assert "pid=" in out["note"]
    # Unknown ids and path-like ids still report plain {"ok": False}.
    for bad in ("r_0000ffff", "../r_0000c003", "C:evil"):
        assert await cancel_reminder.handle(app_state, reminder_id=bad) == {"ok": False}
    sched.shutdown()


async def test_handlers_without_init(app_state: AppState) -> None:
    # reminders is None on the fresh fixture
    assert app_state.reminders is None
    assert (await set_reminder.handle(app_state, text="x", delay_s=1))["error"] == "internal"
    assert (await cancel_reminder.handle(app_state, reminder_id="r_a"))["error"] == "internal"
    assert (await list_reminders.handle(app_state))["error"] == "internal"
