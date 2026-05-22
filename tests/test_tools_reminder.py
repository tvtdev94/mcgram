"""Reminder tool handlers (set/cancel/list)."""

from __future__ import annotations

from mcgram.reminders import ReminderScheduler
from mcgram.runtime import AppState
from mcgram.tools import cancel_reminder, list_reminders, set_reminder


def _wire(app_state: AppState) -> ReminderScheduler:
    sched = ReminderScheduler(app_state.client, app_state.settings, app_state.audit)
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


async def test_handlers_without_init(app_state: AppState) -> None:
    # reminders is None on the fresh fixture
    assert app_state.reminders is None
    assert (await set_reminder.handle(app_state, text="x", delay_s=1))["error"] == "internal"
    assert (await cancel_reminder.handle(app_state, reminder_id="r_a"))["error"] == "internal"
    assert (await list_reminders.handle(app_state))["error"] == "internal"
