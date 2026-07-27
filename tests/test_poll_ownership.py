"""Multi-instance behaviour: the poll lock guards polling, not the process.

Two Claude Code sessions running mcgram at once is the normal case, not an edge
case. The second instance must boot and stay useful (send-only), and `ask` must
refuse fast there rather than blocking until timeout.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcgram.audit import AuditLog
from mcgram.config import Settings
from mcgram.poll_ownership import PollOwnership
from mcgram.rate_limiter import RateLimiter
from mcgram.runtime import AppState
from mcgram.tools import ask
from mcgram.update_dispatcher import UpdateDispatcher


def _state(settings: Settings, audit: AuditLog) -> AppState:
    client = MagicMock()
    client.answer_callback_query = AsyncMock(return_value=True)
    return AppState(
        settings=settings,
        client=client,
        dispatcher=UpdateDispatcher(),
        rate=RateLimiter(20),
        audit=audit,
    )


def test_second_instance_does_not_own_polling(
    tmp_path: Path, settings: Settings, audit_log: AuditLog
) -> None:
    """First acquires, second is refused — and neither raises."""
    lock = tmp_path / ".lock"
    first, second = PollOwnership(lock), PollOwnership(lock)
    state_a, state_b = _state(settings, audit_log), _state(settings, audit_log)

    assert first.attach(state_a) is True
    assert second.attach(state_b) is False

    assert state_a.owns_polling is True
    assert state_a.ask_registry is not None
    assert state_b.owns_polling is False
    assert state_b.ask_registry is None
    assert state_b.poll_owner_pid == os.getpid()
    first.release()


def test_ownership_transfers_after_owner_releases(
    tmp_path: Path, settings: Settings, audit_log: AuditLog
) -> None:
    """Killing the owning session must hand `ask` to a waiting instance."""
    lock = tmp_path / ".lock"
    first, second = PollOwnership(lock), PollOwnership(lock)
    state_a, state_b = _state(settings, audit_log), _state(settings, audit_log)
    first.attach(state_a)
    second.attach(state_b)
    assert state_b.ask_registry is None

    first.release()
    assert second.attach(state_b) is True
    assert state_b.owns_polling is True
    assert state_b.ask_registry is not None
    second.release()


def test_stale_lock_from_dead_pid_is_reclaimed(
    tmp_path: Path, settings: Settings, audit_log: AuditLog
) -> None:
    """A lock left by a killed instance (or a recycled PID) must not be fatal.

    Pre-0.3.0 this was the `-32000` Windows PID-recycling bug: a stale lock
    blocked startup entirely. Now the dead owner is detected and evicted.
    """
    lock = tmp_path / ".lock"
    lock.write_text("999999", encoding="utf-8")  # almost certainly not running

    ownership = PollOwnership(lock)
    state = _state(settings, audit_log)
    assert ownership.attach(state) is True
    assert state.owns_polling is True
    assert int(lock.read_text()) == os.getpid()
    ownership.release()


def test_skip_lock_lets_both_instances_poll(
    tmp_path: Path, settings: Settings, audit_log: AuditLog
) -> None:
    """MCGRAM_SKIP_LOCK=1 keeps its meaning: bypass entirely, accept 409 risk."""
    lock = tmp_path / ".lock"
    first = PollOwnership(lock, skip_lock=True)
    second = PollOwnership(lock, skip_lock=True)
    state_a, state_b = _state(settings, audit_log), _state(settings, audit_log)

    assert first.attach(state_a) is True
    assert second.attach(state_b) is True
    assert not lock.exists()  # no lock file written at all


def test_registry_reused_and_handler_registered_once(
    tmp_path: Path, settings: Settings, audit_log: AuditLog
) -> None:
    """Re-promotion must not stack duplicate dispatcher handlers."""
    ownership = PollOwnership(tmp_path / ".lock")
    state = _state(settings, audit_log)

    ownership.attach(state)
    registry = state.ask_registry
    ownership.release()
    ownership.sync(state)
    assert state.ask_registry is None

    ownership.attach(state)
    assert state.ask_registry is registry
    assert len(state.dispatcher._handlers) == 1
    ownership.release()


async def test_supervisor_promotes_when_lock_frees(
    tmp_path: Path, settings: Settings, audit_log: AuditLog
) -> None:
    """A degraded instance picks up polling without a restart."""
    lock = tmp_path / ".lock"
    owner = PollOwnership(lock)
    owner.attach(_state(settings, audit_log))

    waiting = PollOwnership(lock)
    state = _state(settings, audit_log)
    waiting.attach(state)
    assert state.owns_polling is False

    polling = asyncio.Event()

    async def fake_poll() -> None:
        polling.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(
        waiting.supervise(state, fake_poll, retry_interval_s=0.01)
    )
    await asyncio.sleep(0.05)
    assert state.owns_polling is False  # still blocked by the owner

    owner.release()
    await asyncio.wait_for(polling.wait(), timeout=2)
    assert state.owns_polling is True
    assert state.ask_registry is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_ask_fails_fast_when_not_polling(
    settings: Settings, audit_log: AuditLog
) -> None:
    """The core bug: `ask` must NOT block for the full timeout in a degraded
    instance. The reply would land in the polling process, so waiting here can
    only ever end in a `timeout` result that misreports what happened."""
    state = _state(settings, audit_log)
    state.owns_polling = False
    state.poll_owner_pid = 999999
    state.settings.defaults.ask_timeout_s = 120

    t0 = time.monotonic()
    out = await ask.handle(state, question="deploy?", options=["Yes", "No"])
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, "ask must not wait on a timeout it cannot win"
    assert out["error"] == "polling_not_owned"
    assert out["poll_owner_pid"] == 999999
    assert out["channel"] == "default"
    state.client.send_message.assert_not_called()  # never touched the network


async def test_ask_fails_fast_reasons_are_distinct(
    settings: Settings, audit_log: AuditLog
) -> None:
    """Contention, config, and missing-bot are three different problems."""
    state = _state(settings, audit_log)
    assert state.settings.bot is not None

    state.settings.bot.disable_polling = True
    out = await ask.handle(state, question="?")
    assert out["error"] == "polling_disabled"

    state.settings.bot.disable_polling = False
    state.client = None
    out = await ask.handle(state, question="?")
    assert out["error"] == "telegram_not_configured"


async def test_degraded_instance_can_still_send(
    settings: Settings, audit_log: AuditLog, httpx_mock
) -> None:
    """Losing the poll lock must not cost send_message — it's one-way HTTP."""
    from mcgram.tg_client import TelegramClient
    from mcgram.tools import send_message

    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/sendMessage",
        json={"ok": True, "result": {"message_id": 5}},
    )
    async with TelegramClient("fake-token", api_root=settings.api_root) as client:
        state = AppState(
            settings=settings, client=client, dispatcher=UpdateDispatcher(),
            rate=RateLimiter(20), audit=audit_log,
        )
        state.owns_polling = False  # degraded
        out = await send_message.handle(state, text="build passed")
    assert out["ok"] is True


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (None, 30.0),        # unset → production cadence
        ("0.5", 0.5),        # valid override
        ("abc", 30.0),       # garbage → fall back, never crash the supervisor
        ("0", 30.0),         # zero would busy-loop the retry
        ("-5", 30.0),        # negative is meaningless as a sleep
        ("  2  ", 2.0),      # tolerate whitespace
    ],
)
def test_retry_interval_override(
    monkeypatch: pytest.MonkeyPatch, env: str | None, expected: float
) -> None:
    """A bad override must degrade to the default, not break the retry loop."""
    from mcgram.poll_ownership import RETRY_INTERVAL_ENV, retry_interval_s_default

    if env is None:
        monkeypatch.delenv(RETRY_INTERVAL_ENV, raising=False)
    else:
        monkeypatch.setenv(RETRY_INTERVAL_ENV, env)
    assert retry_interval_s_default() == expected


def test_degraded_mode_does_not_mutate_disable_polling(
    tmp_path: Path, settings: Settings, audit_log: AuditLog
) -> None:
    """Degraded ≠ disable_polling.

    `Settings._seed_default_channel` reads `disable_polling` to decide whether
    `channels.default` is telegram or ntfy. Implementing degraded mode by
    flipping that flag at runtime would silently reroute every send to a
    different transport. Losing the poll lock must cost `ask` and nothing else.
    """
    assert settings.bot is not None
    before = settings.bot.disable_polling
    default_before = settings.channels["default"].transport

    second = PollOwnership(tmp_path / ".lock")
    holder = PollOwnership(tmp_path / ".lock")
    holder.attach(_state(settings, audit_log))
    state = _state(settings, audit_log)
    assert second.attach(state) is False

    assert settings.bot.disable_polling is before
    assert settings.channels["default"].transport == default_before == "telegram"
    assert state.settings.resolve_destination(None).transport == "telegram"
    holder.release()


async def test_degraded_instance_can_still_set_reminders(
    settings: Settings, audit_log: AuditLog
) -> None:
    """Reminders send over one-way HTTP, so they work without the poll lock."""
    from mcgram.reminders import ReminderScheduler

    state = _state(settings, audit_log)
    state.owns_polling = False
    scheduler = ReminderScheduler(state.client, settings, audit_log)
    out = scheduler.create("check logs", delay_s=60)
    assert out["reminder_id"].startswith("r_")
    assert len(scheduler.list()) == 1
    scheduler.shutdown()
