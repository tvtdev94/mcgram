"""`ask` across two sessions through the broker, at the tool boundary.

Two `AppState`s share one spool directory: the "owner" runs the real
`serve_while_owner` supervisor around a real `AskRegistry`; the "requester" has
no registry and calls the real `ask.handle`. Only the Telegram client is faked,
and the requester's fake doubles as a tripwire — it must never be touched.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest

from mcgram import ask_via_broker
from mcgram.ask_broker import HEARTBEAT_KEY, HEARTBEAT_KIND, PROTOCOL_VERSION
from mcgram.ask_registry import AskRegistry
from mcgram.audit import AuditLog
from mcgram.broker_wiring import ASK_NAMESPACE, serve_while_owner, start_ask_broker
from mcgram.config import Settings
from mcgram.poll_ownership import cancel_task
from mcgram.rate_limiter import RateLimiter
from mcgram.runtime import AppState
from mcgram.spool import Spool
from mcgram.tools import ask
from mcgram.update_dispatcher import UpdateDispatcher
from tests.test_ask_server import FakeClient

OPERATOR = 42


def _settings(tmp_path: Path, *, enabled: bool = True) -> Settings:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
bot:
  token_env: TEST_TOKEN
  operator_chat_id: {OPERATOR}
channels:
  side:
    transport: telegram
    chat_id: 7
audit:
  path: {(tmp_path / 'audit.jsonl').as_posix()}
broker:
  enabled: {str(enabled).lower()}
  spool_dir: {(tmp_path / 'spool').as_posix()}
  poll_interval_s: 0.02
""",
        encoding="utf-8",
    )
    return Settings.load(cfg)


def _state(settings: Settings, *, rate_per_min: int = 20) -> AppState:
    return AppState(
        settings=settings,
        dispatcher=UpdateDispatcher(),
        rate=RateLimiter(rate_per_min),
        audit=AuditLog(settings.audit.path),
        client=FakeClient(),  # type: ignore[arg-type]
    )


def _hold_lock(settings: Settings, pid: int | None = None) -> None:
    """Both states live in this process, so the owner's pid is ours."""
    settings.lock_path.write_text(str(pid or os.getpid()), encoding="utf-8")


async def _until(predicate: Any, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.fixture
async def pair(tmp_path: Path):
    """(requester, owner, registry) with the owner supervisor running."""
    settings = _settings(tmp_path)
    _hold_lock(settings)
    requester, owner = _state(settings), _state(settings)
    task = start_ask_broker(requester)
    await cancel_task(task)  # requester is never the owner; keep only its client half
    assert requester.ask_broker is not None
    registry = AskRegistry()
    registry.set_answer_hook(owner.client.answer_callback_query)  # type: ignore[union-attr]
    owner.ask_registry = registry
    spool = Spool(settings.spool_root, ASK_NAMESPACE)
    serving = asyncio.create_task(serve_while_owner(owner, spool))
    assert await _until(lambda: requester.ask_broker.owner_alive()[0])
    try:
        yield requester, owner, registry
    finally:
        await cancel_task(serving)


async def _operator(registry: AskRegistry, update: Any) -> None:
    await _until(lambda: bool(registry._pending))
    q_id = next(iter(registry._pending))
    await registry.handle_update(update(q_id))


async def test_button_answer_reaches_the_non_polling_session(pair: Any) -> None:
    requester, owner, registry = pair
    tap = asyncio.create_task(_operator(
        registry, lambda q: {"callback_query": {"id": "cb", "data": f"{q}:1"}}))
    out = await ask.handle(requester, question="Ship it?", options=["Yes", "No"], timeout_s=5)
    await tap
    assert out["value"] == "No"
    assert out["source"] == "button"
    assert out["via"] == "broker"
    assert owner.client.sent[0]["text"] == "Ship it?"
    assert requester.client.sent == []  # only the owner ever posts


async def test_multi_select_and_columns_travel_through_the_broker(pair: Any) -> None:
    requester, owner, registry = pair

    async def tick_two_then_done() -> None:
        await _until(lambda: bool(registry._pending))
        q_id = next(iter(registry._pending))
        for n, suffix in enumerate(("0", "2", "done")):
            await registry.handle_update(
                {"callback_query": {"id": f"cb{n}", "data": f"{q_id}:{suffix}"}})

    taps = asyncio.create_task(tick_two_then_done())
    out = await ask.handle(requester, question="Which envs?", options=["dev", "stg", "prod"],
                           multi_select=True, columns=2, timeout_s=5)
    await taps
    assert (out["value"], out["source"]) == (["dev", "prod"], "button")
    rows = owner.client.sent[0]["reply_markup"]["inline_keyboard"]
    assert [len(r) for r in rows] == [2, 1, 1]  # 2 per row, then the Done row


async def test_freetext_answer_reaches_the_non_polling_session(pair: Any) -> None:
    requester, _owner, registry = pair
    reply = asyncio.create_task(_operator(registry, lambda q: {"message": {
        "chat": {"id": OPERATOR}, "text": "after lunch", "date": int(time.time()) + 5}}))
    out = await ask.handle(requester, question="When?", timeout_s=5)
    await reply
    assert (out["value"], out["source"]) == ("after lunch", "freetext")


@pytest.mark.parametrize(
    ("post_delay_s", "edit_delay_s", "source", "chat_mark"),
    [
        (1.3, 0.0, "timeout", "⏱ timed out"),
        (0.0, 1.5, "button", "✅"),
    ],
    ids=["post-outlasts-the-deadline", "chat-edit-outlasts-the-deadline"],
)
async def test_requester_and_chat_agree_on_the_outcome(
    pair: Any, monkeypatch: pytest.MonkeyPatch,
    post_delay_s: float, edit_delay_s: float, source: str, chat_mark: str,
) -> None:
    """The requester gives up at its own deadline; the chat must never say
    otherwise. Posting or editing can each sleep out a Telegram flood wait,
    so neither may push the owner's verdict past that deadline."""
    requester, owner, registry = pair
    monkeypatch.setattr(ask_via_broker, "ANSWER_GRACE_S", 0.1)
    owner.client.post_delay_s, owner.client.edit_delay_s = post_delay_s, edit_delay_s

    async def operator_taps_once_posted() -> None:
        if await _until(lambda: bool(owner.client.sent)):
            q_id = next(iter(registry._pending))
            await registry.handle_update({"callback_query": {"id": "cb", "data": f"{q_id}:0"}})

    tap = asyncio.create_task(operator_taps_once_posted())
    out = await ask.handle(requester, question="Ship?", options=["Yes"], timeout_s=1)
    await tap
    assert await _until(lambda: any(e["kind"] == "text" for e in owner.client.edited), 5)
    assert out["source"] == source
    [shown] = [e["text"] for e in owner.client.edited if e["kind"] == "text"]
    assert chat_mark in shown


async def test_owner_side_failure_is_reported_as_an_error_not_a_timeout(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _hold_lock(settings)
    requester, owner = _state(settings), _state(settings, rate_per_min=1)
    await cancel_task(start_ask_broker(requester))
    assert owner.rate.try_acquire("ask")  # the bot token's budget is now spent
    owner.ask_registry = AskRegistry()
    serving = asyncio.create_task(
        serve_while_owner(owner, Spool(settings.spool_root, ASK_NAMESPACE)))
    try:
        assert await _until(lambda: requester.ask_broker.owner_alive()[0])
        out = await ask.handle(requester, question="?", timeout_s=5)
    finally:
        await cancel_task(serving)
    assert out["error"] == "rate_limited"
    assert "source" not in out


async def test_owner_shutdown_mid_ask_fails_fast_and_retires_the_question(
    tmp_path: Path,
) -> None:
    """Shutdown stops the broker, then releases the poll lock (server._run).
    The requester must hear `cancelled` at once instead of waiting out its
    timeout and reporting that the operator ignored the question, and the
    question must not keep offering buttons nobody will ever read."""
    settings = _settings(tmp_path)
    _hold_lock(settings)
    requester, owner = _state(settings), _state(settings)
    await cancel_task(start_ask_broker(requester))
    owner.ask_registry = AskRegistry()
    serving = asyncio.create_task(
        serve_while_owner(owner, Spool(settings.spool_root, ASK_NAMESPACE)))
    assert await _until(lambda: requester.ask_broker.owner_alive()[0])
    asking = asyncio.create_task(
        ask.handle(requester, question="Deploy?", options=["Yes"], timeout_s=30))
    assert await _until(lambda: bool(owner.client.sent))

    t0 = time.monotonic()
    await cancel_task(serving)
    settings.lock_path.unlink()
    out = await asyncio.wait_for(asking, timeout=5)

    assert time.monotonic() - t0 < 2
    assert out["error"] == "cancelled"
    assert "stopped" in out["reason"]
    posted = owner.client.sent[0]["message_id"]
    assert {(e["kind"], e["message_id"]) for e in owner.client.edited} == {
        ("text", posted), ("markup", posted)}


# --- fail fast: never post, never wait --------------------------------------


async def _assert_fails_fast(requester: AppState, settings: Settings) -> dict[str, Any]:
    t0 = time.monotonic()
    out = await ask.handle(requester, question="deploy?", options=["Yes"], timeout_s=120)
    assert time.monotonic() - t0 < 1.0
    assert out["error"] == "polling_not_owned"
    assert out["broker"]  # names the concrete broker reason
    assert requester.client.sent == []
    assert Spool(settings.spool_root, ASK_NAMESPACE).keys("request") == set()
    return out


async def test_no_owner_heartbeat_fails_fast(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    requester = _state(settings)
    await cancel_task(start_ask_broker(requester))
    await _assert_fails_fast(requester, settings)


@pytest.mark.parametrize(
    ("heartbeat", "reason"),
    [
        ({"at": time.time() - 3600, "pid": os.getpid()}, "stale"),
        ({"at": time.time(), "pid": 2**22 + 12345}, "no longer running"),
        ({"at": time.time(), "pid": os.getpid(), "protocol": 0}, "protocol"),
    ],
    ids=["stale", "owner-dead", "old-protocol"],
)
async def test_unusable_owner_heartbeat_fails_fast(
    tmp_path: Path, heartbeat: dict[str, Any], reason: str
) -> None:
    settings = _settings(tmp_path)
    requester = _state(settings)
    await cancel_task(start_ask_broker(requester))
    Spool(settings.spool_root, ASK_NAMESPACE).write(
        HEARTBEAT_KEY, HEARTBEAT_KIND,
        {"protocol": PROTOCOL_VERSION, "expires_at": time.time() + 600, **heartbeat})
    out = await _assert_fails_fast(requester, settings)
    assert reason in out["broker"]


async def test_losing_ownership_withdraws_the_heartbeat(pair: Any, tmp_path: Path) -> None:
    """A demoted owner stops reading requests at once; its heartbeat must go
    with it, or requesters would block for `heartbeat_stale_s` (90s)."""
    requester, owner, registry = pair
    owner.ask_registry = None
    assert await _until(lambda: not requester.ask_broker.owner_alive()[0])
    await _assert_fails_fast(requester, requester.settings)

    owner.ask_registry = registry  # re-promotion serves again, no restart
    assert await _until(lambda: requester.ask_broker.owner_alive()[0])


async def test_non_operator_channel_is_rejected_before_writing(pair: Any) -> None:
    requester, owner, _ = pair
    out = await ask.handle(requester, question="?", channel="side", timeout_s=5)
    assert out["error"] == "ask_requires_operator_chat"
    assert owner.client.sent == [] and requester.client.sent == []


# --- broker off or unusable: behaviour as before ----------------------------


async def test_broker_disabled_keeps_the_original_refusal(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=False)
    requester = _state(settings)
    requester.poll_owner_pid = 4242
    assert start_ask_broker(requester) is None
    assert requester.ask_broker is None
    out = await ask.handle(requester, question="?", timeout_s=120)
    assert out["error"] == "polling_not_owned"
    assert "broker" not in out
    assert not (tmp_path / "spool").exists()  # off means the spool is never touched


async def test_unsafe_spool_at_boot_leaves_the_broker_off(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path)
    (tmp_path / "spool").write_text("not a directory", encoding="utf-8")
    requester = _state(settings)
    assert start_ask_broker(requester) is None
    assert requester.ask_broker is None
    assert "broker off" in caplog.text
    out = await ask.handle(requester, question="?", timeout_s=120)
    assert out["error"] == "polling_not_owned"


async def test_spool_write_failure_at_runtime_fails_before_posting(
    pair: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    requester, owner, _ = pair
    monkeypatch.setattr(requester.ask_broker._spool, "write_exclusive",
                        lambda *a, **k: False)
    out = await ask.handle(requester, question="?", timeout_s=5)
    assert out["error"] == "broker_unavailable"
    assert owner.client.sent == [] and requester.client.sent == []
