"""AskServer (owner side) tests, including the broker↔server round trip.

The registry is the real `AskRegistry` throughout — the point of the design is
that a brokered ask is an ordinary pending ask, so stubbing it out would test
nothing. Only the Telegram client is faked.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest

from mcgram.ask_broker import HEARTBEAT_KEY, HEARTBEAT_KIND, PROTOCOL_VERSION, AskBroker
from mcgram.ask_registry import AskRegistry
from mcgram.ask_server import AskServer
from mcgram.audit import AuditLog
from mcgram.config import LimitsConfig
from mcgram.errors import TelegramError, TransportError
from mcgram.rate_limiter import RateLimiter
from mcgram.spool import Spool

Q = "q_abcdef0123456789"


class FakeClient:
    """Records outbound calls; `send_message` can be made to fail on demand."""

    def __init__(self, *, fail_send: Exception | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self._next_message_id = 1000
        self._fail_send = fail_send
        # Stand-ins for a Telegram call that sleeps out a flood wait.
        self.post_delay_s = 0.0
        self.edit_delay_s = 0.0

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(self.post_delay_s)
        if self._fail_send is not None:
            raise self._fail_send
        self._next_message_id += 1
        msg = {"message_id": self._next_message_id, "text": text, "chat_id": chat_id, **kwargs}
        self.sent.append(msg)
        return msg

    async def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: Any = None, **kwargs: Any
    ) -> None:
        self.edited.append({"kind": "markup", "message_id": message_id})

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, **kwargs: Any
    ) -> None:
        await asyncio.sleep(self.edit_delay_s)
        self.edited.append({"kind": "text", "message_id": message_id, "text": text})

    async def answer_callback_query(self, callback_query_id: str) -> bool:
        return True


def _server(
    tmp_path: Path,
    *,
    client: FakeClient | None = None,
    registry: AskRegistry | None = None,
    rate_per_min: int = 100,
    **kw: Any,
) -> tuple[AskServer, Spool, AskRegistry, FakeClient]:
    spool = Spool(tmp_path / "spool", "asks")
    reg = registry or AskRegistry()
    cli = client or FakeClient()
    server = AskServer(
        spool,
        reg,
        cli,  # type: ignore[arg-type]
        RateLimiter(rate_per_min),
        AuditLog(tmp_path / "audit.jsonl"),
        operator_chat_id=42,
        limits=LimitsConfig(),
        poll_interval_s=kw.get("poll_interval_s", 0.01),
        answer_ttl_s=kw.get("answer_ttl_s", 300),
        heartbeat_stale_s=kw.get("heartbeat_stale_s", 90),
        sweep_interval_s=kw.get("sweep_interval_s", 60),
    )
    return server, spool, reg, cli


def _request(spool: Spool, key: str = Q, **kw: Any) -> None:
    now = time.time()
    spool.write(
        key,
        "request",
        {
            "q_id": key,
            "protocol": kw.get("protocol", PROTOCOL_VERSION),
            "question": kw.get("question", "Deploy?"),
            "options": kw.get("options", ["Yes", "No"]),
            "chat_id": kw.get("chat_id", 42),
            "timeout_s": kw.get("timeout_s", 5),
            "requester_pid": 999,
            "created_at": now,
            "expires_at": kw.get("expires_at", now + 60),
            **{k: kw[k] for k in ("multi_select", "columns") if k in kw},
        },
    )


async def _until(predicate: Any, timeout: float = 3.0) -> bool:
    """Poll a condition instead of sleeping a fixed amount — keeps CI honest."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


# --- heartbeat --------------------------------------------------------------


def test_write_heartbeat_records_pid_and_protocol(tmp_path: Path) -> None:
    server, spool, _, _ = _server(tmp_path)
    assert server.write_heartbeat() is True
    hb = spool.read(HEARTBEAT_KEY, HEARTBEAT_KIND)
    assert hb is not None
    assert hb["pid"] == os.getpid()
    assert hb["protocol"] == PROTOCOL_VERSION
    assert hb["expires_at"] > hb["at"]


async def test_run_publishes_heartbeat(tmp_path: Path) -> None:
    server, spool, _, _ = _server(tmp_path)
    task = asyncio.create_task(server.run())
    try:
        assert await _until(lambda: spool.read(HEARTBEAT_KEY, HEARTBEAT_KIND) is not None)
    finally:
        task.cancel()


# --- serving ----------------------------------------------------------------


async def test_serves_a_request_and_writes_the_answer(tmp_path: Path) -> None:
    server, spool, reg, cli = _server(tmp_path)
    _request(spool)
    task = asyncio.create_task(server.run())
    try:
        assert await _until(lambda: bool(cli.sent))
        assert cli.sent[0]["text"] == "Deploy?"
        # Resolve it the way Telegram would: a button tap on the posted question.
        q_id = next(iter(reg._pending))
        await reg.handle_update({"callback_query": {"id": "cb1", "data": f"{q_id}:0"}})
        assert await _until(lambda: spool.read(Q, "answer") is not None)
    finally:
        task.cancel()
    answer = spool.read(Q, "answer")
    assert answer is not None
    assert answer["value"] == "Yes"
    assert answer["source"] == "button"
    assert answer["answered_by_pid"] == os.getpid()
    assert answer["expires_at"] > answer["answered_at"]


async def test_uses_the_existing_registry_so_freetext_still_resolves(tmp_path: Path) -> None:
    """A brokered ask must be an ordinary pending ask — freetext replies land on
    it through the untouched message handler."""
    server, spool, reg, cli = _server(tmp_path)
    _request(spool, options=None)
    task = asyncio.create_task(server.run())
    try:
        assert await _until(lambda: bool(cli.sent))
        await reg.handle_update({
            "message": {"chat": {"id": 42}, "text": "ship it", "date": time.time() + 10}
        })
        assert await _until(lambda: spool.read(Q, "answer") is not None)
    finally:
        task.cancel()
    answer = spool.read(Q, "answer")
    assert answer is not None
    assert answer["value"] == "ship it"
    assert answer["source"] == "freetext"


async def test_expired_request_is_never_served(tmp_path: Path) -> None:
    server, spool, _, cli = _server(tmp_path)
    _request(spool, expires_at=time.time() - 1)
    task = asyncio.create_task(server.run())
    try:
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
    assert cli.sent == []
    assert spool.keys("taken") == set()


async def test_request_with_wrong_protocol_is_skipped(tmp_path: Path) -> None:
    server, spool, _, cli = _server(tmp_path)
    _request(spool, protocol=PROTOCOL_VERSION + 1)
    task = asyncio.create_task(server.run())
    try:
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
    assert cli.sent == []


@pytest.mark.parametrize(
    "bad",
    [
        {"question": ""},
        {"question": 123},
        {"chat_id": "42"},
        {"timeout_s": 0},
        {"timeout_s": "5"},
        {"options": [1, 2]},
        {"options": "Yes"},
    ],
)
async def test_malformed_request_is_skipped_not_raised(tmp_path: Path, bad: dict) -> None:
    """Requests are files on disk; a truncated or hand-edited one must not take
    down the watch loop."""
    server, spool, _, cli = _server(tmp_path)
    _request(spool, **bad)
    task = asyncio.create_task(server.run())
    try:
        await asyncio.sleep(0.1)
        assert not task.done()
    finally:
        task.cancel()
    assert cli.sent == []


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        ({"chat_id": 7}, "operator chat"),
        ({"options": [str(n) for n in range(7)]}, "ask_options_max"),
        ({"options": ["x" * 4097]}, "label"),
        ({"question": "q" * 4097}, "question"),
        ({"timeout_s": 601}, "ask_timeout_max_s"),
        ({"multi_select": True, "options": None}, "multi_select"),
    ],
    ids=["foreign-chat", "too-many-options", "long-label", "long-question",
         "long-timeout", "multi-select-without-options"],
)
async def test_request_outside_the_owners_limits_is_refused_with_an_answer(
    tmp_path: Path, bad: dict, why: str
) -> None:
    """Any process running as this user can write a request. The owner holds
    it to its own config — not the requester's — and says why it refused, so
    the requester fails at once instead of waiting out its timeout."""
    server, spool, _, cli = _server(tmp_path)
    _request(spool, **bad)
    task = asyncio.create_task(server.run())
    try:
        assert await _until(lambda: spool.read(Q, "answer") is not None)
    finally:
        task.cancel()
    answer = spool.read(Q, "answer")
    assert answer is not None and answer["source"] == "invalid_request"
    assert why in answer["detail"]
    assert cli.sent == []


# --- exclusivity ------------------------------------------------------------


async def test_two_servers_on_one_spool_serve_a_request_once(tmp_path: Path) -> None:
    """MCGRAM_SKIP_LOCK=1 lets two owners coexist; the claim is what stops the
    operator seeing the same question twice."""
    server_a, spool, _, cli_a = _server(tmp_path)
    shared = Spool(tmp_path / "spool", "asks")
    cli_b = FakeClient()
    server_b = AskServer(
        shared,
        AskRegistry(),
        cli_b,  # type: ignore[arg-type]
        RateLimiter(100),
        AuditLog(tmp_path / "audit.jsonl"),
        operator_chat_id=42,
        limits=LimitsConfig(),
        poll_interval_s=0.01,
        answer_ttl_s=300,
        heartbeat_stale_s=90,
    )
    _request(spool)
    tasks = [asyncio.create_task(server_a.run()), asyncio.create_task(server_b.run())]
    try:
        assert await _until(lambda: bool(cli_a.sent) or bool(cli_b.sent))
        await asyncio.sleep(0.15)
    finally:
        for t in tasks:
            t.cancel()
    assert len(cli_a.sent) + len(cli_b.sent) == 1


async def test_a_request_is_not_served_twice_by_one_server(tmp_path: Path) -> None:
    server, spool, _, cli = _server(tmp_path)
    _request(spool, timeout_s=2)
    task = asyncio.create_task(server.run())
    try:
        assert await _until(lambda: bool(cli.sent))
        await asyncio.sleep(0.15)  # several more ticks over the same request
    finally:
        task.cancel()
    assert len(cli.sent) == 1


# --- failure paths ----------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "source", "detail"),
    [
        (TelegramError(400, "bad"), "telegram_error", "bad"),
        (TransportError("telegram", "ConnectError: refused"), "transport_unreachable",
         "ConnectError: refused"),
    ],
)
async def test_telegram_failure_still_writes_an_answer(
    tmp_path: Path, error: Exception, source: str, detail: str
) -> None:
    """Otherwise the requester waits out its timeout and reports `timeout`,
    telling Claude the operator ignored a question Telegram never delivered."""
    server, spool, _, _ = _server(tmp_path, client=FakeClient(fail_send=error))
    _request(spool)
    task = asyncio.create_task(server.run())
    try:
        assert await _until(lambda: spool.read(Q, "answer") is not None)
    finally:
        task.cancel()
    answer = spool.read(Q, "answer")
    assert answer is not None
    assert answer["source"] == source
    assert answer["detail"] == detail


async def test_unexpected_failure_still_writes_an_answer(tmp_path: Path) -> None:
    server, spool, _, _ = _server(tmp_path, client=FakeClient(fail_send=RuntimeError("boom")))
    _request(spool)
    task = asyncio.create_task(server.run())
    try:
        assert await _until(lambda: spool.read(Q, "answer") is not None)
    finally:
        task.cancel()
    answer = spool.read(Q, "answer")
    assert answer is not None and answer["source"] == "internal_error"


async def test_rate_limit_answers_rather_than_hanging(tmp_path: Path) -> None:
    server, spool, _, cli = _server(tmp_path, rate_per_min=1)
    _request(spool, key=Q)
    _request(spool, key="q_bbbbbbbb22222222")
    task = asyncio.create_task(server.run())
    try:
        assert await _until(lambda: spool.read("q_bbbbbbbb22222222", "answer") is not None
                            or spool.read(Q, "answer") is not None)
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
    sources = [
        (spool.read(k, "answer") or {}).get("source")
        for k in (Q, "q_bbbbbbbb22222222")
    ]
    assert "rate_limited" in sources


async def test_a_tick_error_does_not_kill_the_loop(tmp_path: Path) -> None:
    """A dead watcher is indistinguishable from "no owner" to every other
    session, so the loop must survive its own bugs."""
    server, spool, _, _ = _server(tmp_path)
    calls = {"n": 0}
    original = server._scan_once

    def explode() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first tick blows up")
        original()

    server._scan_once = explode  # type: ignore[method-assign]
    task = asyncio.create_task(server.run())
    try:
        assert await _until(lambda: calls["n"] >= 3)
        assert not task.done()
    finally:
        task.cancel()


async def test_a_slow_ask_does_not_block_another(tmp_path: Path) -> None:
    server, spool, reg, cli = _server(tmp_path)
    _request(spool, key=Q, timeout_s=30, question="slow")
    _request(spool, key="q_bbbbbbbb22222222", timeout_s=30, question="fast")
    task = asyncio.create_task(server.run())
    try:
        assert await _until(lambda: len(cli.sent) == 2)
        # Answer only the second one; the first stays pending.
        pending = {p.question_id: p for p in reg._pending.values()}
        fast = next(
            q for q, p in pending.items()
            if cli.sent[[m["message_id"] for m in cli.sent].index(p.message_id)]["text"] == "fast"
        )
        await reg.handle_update({"callback_query": {"id": "cb", "data": f"{fast}:0"}})
        assert await _until(lambda: spool.read("q_bbbbbbbb22222222", "answer") is not None)
    finally:
        task.cancel()
    assert spool.read(Q, "answer") is None


# --- sweep ------------------------------------------------------------------


async def test_run_sweeps_stale_records(tmp_path: Path) -> None:
    server, spool, _, _ = _server(tmp_path, sweep_interval_s=0)
    spool.write("q_cccccccc33333333", "answer", {"expires_at": time.time() - 10})
    task = asyncio.create_task(server.run())
    try:
        assert await _until(lambda: spool.keys("answer") == set())
    finally:
        task.cancel()


async def test_sweep_does_not_touch_live_records(tmp_path: Path) -> None:
    server, spool, _, _ = _server(tmp_path, sweep_interval_s=0)
    _request(spool)
    task = asyncio.create_task(server.run())
    try:
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
    assert spool.read(Q, "taken") is not None


# --- round trip -------------------------------------------------------------


async def test_broker_and_server_complete_a_round_trip(tmp_path: Path) -> None:
    """The phase's central claim: a session without the poll lock can ask, and
    the session with it answers on their behalf."""
    server, spool, reg, cli = _server(tmp_path)
    lock_path = tmp_path / ".lock"
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    broker = AskBroker(
        Spool(tmp_path / "spool", "asks"),
        lock_path,
        poll_interval_s=0.01,
        ttl_pad_s=30,
        heartbeat_stale_s=90,
    )

    task = asyncio.create_task(server.run())
    try:
        assert await _until(lambda: broker.owner_alive()[0])
        assert broker.open_request(
            Q, question="Ship it?", options=["Yes", "No"], chat_id=42, timeout_s=10
        )

        async def operator_taps() -> None:
            await _until(lambda: bool(reg._pending))
            q_id = next(iter(reg._pending))
            await reg.handle_update({"callback_query": {"id": "cb", "data": f"{q_id}:1"}})

        tap = asyncio.create_task(operator_taps())
        answer = await broker.wait_for_answer(Q, timeout_s=5)
        await tap
    finally:
        task.cancel()

    assert answer is not None
    assert answer["value"] == "No"
    assert answer["source"] == "button"
    broker.close_request(Q)
    assert spool.keys("request") == set()


async def test_round_trip_times_out_cleanly_when_nobody_answers(tmp_path: Path) -> None:
    server, spool, _, cli = _server(tmp_path)
    lock_path = tmp_path / ".lock"
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    broker = AskBroker(
        Spool(tmp_path / "spool", "asks"),
        lock_path,
        poll_interval_s=0.01,
        ttl_pad_s=30,
        heartbeat_stale_s=90,
    )
    task = asyncio.create_task(server.run())
    try:
        broker.open_request(Q, question="?", options=None, chat_id=42, timeout_s=1)
        assert await _until(lambda: bool(cli.sent))
        answer = await broker.wait_for_answer(Q, timeout_s=3)
    finally:
        task.cancel()
    assert answer is not None
    assert answer["source"] == "timeout"
    assert answer["value"] == ""


async def test_request_that_could_not_be_deleted_is_never_served_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows another process reading the request file makes its unlink
    fail (WinError 32). The claim must outlive it, or the next scan sees an
    unclaimed request and posts the same question a second time."""
    server, spool, reg, cli = _server(tmp_path)
    lock_path = tmp_path / ".lock"
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    broker = AskBroker(Spool(tmp_path / "spool", "asks"), lock_path,
                       poll_interval_s=0.01, ttl_pad_s=30, heartbeat_stale_s=90)
    task = asyncio.create_task(server.run())
    try:
        assert broker.open_request(Q, question="?", options=["Yes"], chat_id=42, timeout_s=10)
        assert await _until(lambda: bool(reg._pending))
        q_id = next(iter(reg._pending))
        await reg.handle_update({"callback_query": {"id": "cb", "data": f"{q_id}:0"}})
        assert await broker.wait_for_answer(Q, timeout_s=3) is not None

        real_unlink = Path.unlink

        def busy_request(self: Path, missing_ok: bool = False) -> None:
            if self.name.endswith(".request.json"):
                raise PermissionError(32, "being used by another process", str(self))
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", busy_request)
        broker.close_request(Q)
        assert spool.read(Q, "request") is not None  # the failure really happened
        await asyncio.sleep(0.15)  # many scans over the surviving request
    finally:
        task.cancel()
    assert len(cli.sent) == 1
