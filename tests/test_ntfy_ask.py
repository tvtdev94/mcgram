"""ask over ntfy: answer matching, listener lifecycle, and failure reporting.

Everything runs against the scripted ntfy in `fake_ntfy`, at the HTTP boundary.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import anyio
import pytest

from mcgram.audit import AuditLog
from mcgram.ntfy_ask import (
    NtfyAsk,
    NtfyAuthFailedError,
    NtfyReplyUnreachableError,
    NtfyTopicNotProtectedError,
)
from mcgram.ntfy_client import NtfyClient
from tests.fake_ntfy import (
    OPEN_TIME,
    QUESTION_TOPIC,
    REPLY_TOPIC,
    SERVER,
    TOKEN,
    FakeNtfy,
)

OPTIONS = ["Prod", "Staging", "Cancel"]


@pytest.fixture
def fake(httpx_mock: Any) -> Any:
    f = FakeNtfy(httpx_mock)
    yield f
    assert f.unexpected == []


@pytest.fixture
async def client() -> Any:
    async with NtfyClient(SERVER, access_token=TOKEN) as c:
        yield c


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
async def asker(client: NtfyClient, audit: AuditLog) -> Any:
    a = NtfyAsk(client, server=SERVER, reply_topic=REPLY_TOPIC, audit=audit)
    yield a
    await a.shutdown()


def _ask(asker: NtfyAsk, *, timeout_s: float = 10.0) -> asyncio.Task[dict[str, Any]]:
    return asyncio.create_task(asker.open(QUESTION_TOPIC, "Deploy?", OPTIONS, timeout_s))


def _body(question: dict[str, Any], idx: int) -> str:
    """What the phone POSTs to the reply topic when option `idx` is tapped."""
    return str(question["actions"][idx]["body"])


def _audit_text(audit: AuditLog) -> str:
    p = Path(audit.path)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _audit_lines(audit: AuditLog) -> list[dict[str, Any]]:
    return [json.loads(line) for line in _audit_text(audit).splitlines()]


async def test_tap_resolves_with_its_label_and_the_button_carries_no_credential(
    asker: NtfyAsk, fake: FakeNtfy, audit: AuditLog
) -> None:
    task = _ask(asker)
    _, question = await fake.next_question()
    fake.broadcast(_body(question, 1))
    out = await task

    assert (out["value"], out["source"]) == ("Staging", "button")
    actions = question["actions"]
    assert [a["label"] for a in actions] == OPTIONS
    for a in actions:
        assert (a["action"], a["method"], a["url"]) == (
            "http", "POST", f"{SERVER}/{REPLY_TOPIC}")
        assert "headers" not in a
    bodies = [json.loads(a["body"]) for a in actions]
    assert {b["q"] for b in bodies} == {out["question_id"]}
    assert len({b["t"] for b in bodies}) == len(OPTIONS)
    assert TOKEN not in json.dumps(question)
    assert question["sequence_id"] == out["question_id"]
    # The question is replaced in place: same sequence id, no buttons, silent.
    _, marker = fake.published[-1]
    assert marker["sequence_id"] == question["sequence_id"]
    assert marker["priority"] == 2
    assert "actions" not in marker
    assert "Staging" in marker["message"]
    assert TOKEN not in _audit_text(audit)


async def test_outcome_of_a_maximum_size_question_stays_within_ntfy_message_limit(
    asker: NtfyAsk, fake: FakeNtfy
) -> None:
    # Past 4096 bytes ntfy turns a message into an attachment, so an outcome
    # that quoted the whole question would not replace the notification.
    question = "é" * 2048  # exactly 4096 UTF-8 bytes
    task = asyncio.create_task(asker.open(QUESTION_TOPIC, question, OPTIONS, 10))
    _, posted = await fake.next_question()
    fake.broadcast(_body(posted, 1))
    await task
    _, marker = fake.published[-1]
    assert len(marker["message"].encode("utf-8")) <= 4096
    assert marker["message"].startswith("éé")
    assert marker["message"].endswith("Staging")


@pytest.mark.parametrize(
    ("forge", "audited"),
    [
        (lambda q, toks: json.dumps({"q": q, "t": "A" * 22}), True),
        (lambda q, toks: json.dumps({"q": "q_00000000", "t": toks[0]}), False),
        (lambda q, toks: "yes", False),
        (lambda q, toks: json.dumps({"q": q, "t": toks[0], "pad": "x" * 1100}), False),
    ],
    ids=["wrong-token", "unknown-question", "not-json", "over-1KiB"],
)
async def test_forged_replies_never_resolve_and_only_a_bad_token_is_audited(
    asker: NtfyAsk, fake: FakeNtfy, audit: AuditLog, forge: Any, audited: bool
) -> None:
    task = _ask(asker)
    _, question = await fake.next_question()
    bodies = [json.loads(a["body"]) for a in question["actions"]]
    q_id, tokens = bodies[0]["q"], [b["t"] for b in bodies]
    # A forged reply, then a real tap: the answer must be the real one.
    fake.broadcast(forge(q_id, tokens))
    fake.broadcast(_body(question, 2))
    out = await task

    assert out["value"] == "Cancel"
    rejected = [(r["tool"], r["reason"], r["question_id"])
                for r in _audit_lines(audit) if r.get("status") == "rejected"]
    assert rejected == ([("_ntfy_reply", "bad_token", q_id)] if audited else [])
    assert not any(t in _audit_text(audit) for t in tokens)


async def test_replayed_reply_from_an_earlier_question_is_ignored(
    asker: NtfyAsk, fake: FakeNtfy
) -> None:
    fake.connections = ["live", "live"]  # each question subscribes afresh
    first = _ask(asker)
    _, q1 = await fake.next_question()
    fake.broadcast(_body(q1, 0))
    assert (await first)["value"] == "Prod"

    second = _ask(asker)
    _, q2 = await fake.next_question()
    fake.broadcast(_body(q1, 0))  # the cached reply comes round again
    fake.broadcast(_body(q2, 1))
    assert (await second)["value"] == "Staging"


async def test_two_sessions_on_one_reply_topic_resolve_only_their_own(
    asker: NtfyAsk, fake: FakeNtfy, client: NtfyClient, audit: AuditLog, tmp_path: Path
) -> None:
    other_audit = AuditLog(tmp_path / "other.jsonl")
    other = NtfyAsk(client, server=SERVER, reply_topic=REPLY_TOPIC, audit=other_audit)
    fake.connections = ["live", "live"]
    try:
        mine = _ask(asker)
        _, q_mine = await fake.next_question()
        theirs = _ask(other)
        _, q_theirs = await fake.next_question()
        await fake.wait_streams(2)
        fake.broadcast(_body(q_theirs, 1))
        fake.broadcast(_body(q_mine, 0))
        assert (await mine)["value"] == "Prod"
        assert (await theirs)["value"] == "Staging"
    finally:
        await other.shutdown()
    # Another session's question id is foreign, not forged: never audited.
    for log in (audit, other_audit):
        assert [r for r in _audit_lines(log) if r.get("status") == "rejected"] == []


@pytest.mark.parametrize("public", [QUESTION_TOPIC, REPLY_TOPIC])
async def test_anonymously_readable_topic_refuses_before_posting(
    asker: NtfyAsk, fake: FakeNtfy, public: str
) -> None:
    fake.probe_status[public] = 200
    with pytest.raises(NtfyTopicNotProtectedError) as exc:
        await asker.open(QUESTION_TOPIC, "Deploy?", OPTIONS, 5)
    assert exc.value.topic == public
    assert fake.published == []
    assert fake.stream_since == []


async def test_listener_that_never_connects_refuses_before_posting(
    asker: NtfyAsk, fake: FakeNtfy
) -> None:
    fake.connections = []
    with pytest.raises(NtfyReplyUnreachableError) as exc:
        await asker.open(QUESTION_TOPIC, "Deploy?", OPTIONS, 0.5)
    assert exc.value.posted is False
    assert fake.published == []


@pytest.mark.parametrize(
    ("seen_before_drop", "resume_since"),
    [(False, str(OPEN_TIME)), (True, "m_other")],
    ids=["resume-from-open-time", "resume-from-last-message"],
)
async def test_dropped_subscription_resumes_from_its_cursor(
    asker: NtfyAsk, fake: FakeNtfy, seen_before_drop: bool, resume_since: str
) -> None:
    fake.connections = ["live", "live"]
    task = _ask(asker)
    _, question = await fake.next_question()
    if seen_before_drop:
        fake.streams[0].push(fake.message('{"q":"q_00000000","t":"x"}', msg_id="m_other"))
    fake.streams[0].fail()
    await fake.wait_streams(2)
    fake.streams[1].push(fake.message(_body(question, 2)))
    assert (await task)["value"] == "Cancel"
    assert fake.stream_since == [None, resume_since]


async def test_deadline_catch_up_delivers_a_tap_missed_while_disconnected(
    asker: NtfyAsk, fake: FakeNtfy
) -> None:
    task = _ask(asker, timeout_s=1)
    _, question = await fake.next_question()
    fake.streams[0].fail()
    fake.on_poll = lambda: [fake.message(_body(question, 1))]
    out = await task
    assert (out["value"], out["source"]) == ("Staging", "button")
    assert fake.poll_since == [str(OPEN_TIME)]


async def test_unreachable_reply_topic_at_the_deadline_is_not_a_timeout(
    asker: NtfyAsk, fake: FakeNtfy
) -> None:
    task = _ask(asker, timeout_s=1)
    _, question = await fake.next_question()
    fake.streams[0].fail()
    fake.on_poll = lambda: 503
    with pytest.raises(NtfyReplyUnreachableError) as exc:
        await task
    assert exc.value.posted is True
    _, marker = fake.published[-1]
    assert marker["sequence_id"] == question["sequence_id"]
    assert "stopped" in marker["message"]


async def test_no_tap_on_a_reachable_reply_topic_times_out(
    asker: NtfyAsk, fake: FakeNtfy
) -> None:
    task = _ask(asker, timeout_s=0.3)
    _, question = await fake.next_question()
    out = await task
    assert (out["value"], out["source"]) == ("", "timeout")
    assert fake.poll_since == [str(OPEN_TIME)]
    _, marker = fake.published[-1]
    assert marker["sequence_id"] == question["sequence_id"]
    assert "timed out" in marker["message"]


async def test_hanging_catch_up_read_cannot_hold_the_ask_past_its_bound(
    asker: NtfyAsk, fake: FakeNtfy
) -> None:
    fake.poll_hangs = True
    started = time.monotonic()
    task = _ask(asker, timeout_s=0.3)
    await fake.next_question()
    out = await asyncio.wait_for(task, timeout=15)
    assert (out["value"], out["source"]) == ("", "timeout")
    assert fake.poll_since == [str(OPEN_TIME)]  # the read was made, then given up
    assert time.monotonic() - started < 0.3 + 5 + 2


async def test_auth_failure_on_the_subscription_fails_the_ask_at_once(
    asker: NtfyAsk, fake: FakeNtfy, audit: AuditLog
) -> None:
    fake.connections = ["live", 403]
    started = time.monotonic()
    task = _ask(asker, timeout_s=30)
    await fake.next_question()
    fake.streams[0].fail()
    with pytest.raises(NtfyAuthFailedError) as exc:
        await task
    assert time.monotonic() - started < 10
    assert exc.value.posted is True
    assert ("_ntfy_reply", "auth") in [
        (r["tool"], r.get("reason")) for r in _audit_lines(audit) if r["status"] == "error"]


async def test_cancelled_call_retires_the_question_and_leaves_no_task(
    asker: NtfyAsk, fake: FakeNtfy
) -> None:
    # MCP cancels a call (Esc, or the session closing) through an anyio cancel
    # scope, which re-cancels every await inside it, the cleanup included.
    async with anyio.create_task_group() as tg:
        tg.start_soon(asker.open, QUESTION_TOPIC, "Deploy?", OPTIONS, 30)
        _, question = await fake.next_question()
        tg.cancel_scope.cancel()
    assert len(fake.published) == 2, "the question was never retired"
    _, marker = fake.published[-1]
    assert marker["sequence_id"] == question["sequence_id"]
    assert "stopped" in marker["message"]
    current = asyncio.current_task()
    assert [t for t in asyncio.all_tasks() if t is not current and not t.done()] == []


async def test_shutdown_retires_the_open_question_and_leaves_no_task(
    asker: NtfyAsk, fake: FakeNtfy
) -> None:
    task = _ask(asker, timeout_s=30)
    _, question = await fake.next_question()
    await asker.shutdown()
    assert task.cancelled()
    _, marker = fake.published[-1]
    assert marker["sequence_id"] == question["sequence_id"]
    assert "stopped" in marker["message"]
    current = asyncio.current_task()
    assert [t for t in asyncio.all_tasks() if t is not current and not t.done()] == []
