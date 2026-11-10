"""AskBroker (non-owner side) unit tests: liveness, provenance, wait semantics."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest

from mcgram.ask_broker import (
    HEARTBEAT_KEY,
    HEARTBEAT_KIND,
    PROTOCOL_VERSION,
    AskBroker,
)
from mcgram.spool import Spool

Q = "q_abcdef0123456789"


def _make(tmp_path: Path, **kw: Any) -> tuple[AskBroker, Spool, Path]:
    spool = Spool(tmp_path / "spool", "asks")
    lock_path = tmp_path / ".lock"
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    broker = AskBroker(
        spool,
        lock_path,
        poll_interval_s=kw.get("poll_interval_s", 0.01),
        ttl_pad_s=kw.get("ttl_pad_s", 30),
        heartbeat_stale_s=kw.get("heartbeat_stale_s", 90),
    )
    return broker, spool, lock_path


def _heartbeat(spool: Spool, **kw: Any) -> None:
    now = time.time()
    spool.write(
        HEARTBEAT_KEY,
        HEARTBEAT_KIND,
        {
            "pid": kw.get("pid", os.getpid()),
            "at": kw.get("at", now),
            "protocol": kw.get("protocol", PROTOCOL_VERSION),
            "expires_at": now + 90,
        },
    )


def _answer(spool: Spool, *, value: str = "Yes", pid: int | None = None, **kw: Any) -> None:
    now = time.time()
    payload = {
        "value": value,
        "source": kw.get("source", "button"),
        "answered_at": now,
        "expires_at": now + 300,
    }
    if pid is not None:
        payload["answered_by_pid"] = pid
    payload.update(kw.get("extra", {}))
    spool.write(Q, "answer", payload)


# --- owner_alive ------------------------------------------------------------


def test_owner_alive_false_when_no_heartbeat(tmp_path: Path) -> None:
    broker, _, _ = _make(tmp_path)
    alive, reason = broker.owner_alive()
    assert alive is False
    assert "no mcgram instance" in reason


def test_owner_alive_true_with_fresh_heartbeat(tmp_path: Path) -> None:
    broker, spool, _ = _make(tmp_path)
    _heartbeat(spool)
    assert broker.owner_alive() == (True, "")


def test_owner_alive_false_when_heartbeat_is_stale(tmp_path: Path) -> None:
    """An owner stuck in 409 backoff still holds the lock but reads nothing."""
    broker, spool, _ = _make(tmp_path, heartbeat_stale_s=90)
    _heartbeat(spool, at=time.time() - 200)
    alive, reason = broker.owner_alive()
    assert alive is False
    assert "stale" in reason


def test_owner_alive_false_when_owner_pid_is_dead(tmp_path: Path) -> None:
    broker, spool, _ = _make(tmp_path)
    _heartbeat(spool, pid=999_999_999)
    alive, reason = broker.owner_alive()
    assert alive is False
    assert "no longer running" in reason


def test_owner_alive_false_on_protocol_mismatch(tmp_path: Path) -> None:
    """One session upgraded, the other not — the old owner never reads requests."""
    broker, spool, _ = _make(tmp_path)
    _heartbeat(spool, protocol=PROTOCOL_VERSION + 1)
    alive, reason = broker.owner_alive()
    assert alive is False
    assert "protocol" in reason


def test_owner_alive_false_when_protocol_field_absent(tmp_path: Path) -> None:
    broker, spool, _ = _make(tmp_path)
    spool.write(HEARTBEAT_KEY, HEARTBEAT_KIND, {"pid": os.getpid(), "at": time.time(),
                                                "expires_at": time.time() + 90})
    assert broker.owner_alive()[0] is False


def test_owner_alive_false_on_malformed_timestamp(tmp_path: Path) -> None:
    broker, spool, _ = _make(tmp_path)
    _heartbeat(spool)
    spool.write(HEARTBEAT_KEY, HEARTBEAT_KIND, {"pid": os.getpid(), "at": "now",
                                                "protocol": PROTOCOL_VERSION,
                                                "expires_at": time.time() + 90})
    alive, reason = broker.owner_alive()
    assert alive is False
    assert "malformed" in reason


def test_owner_alive_is_fast_when_nobody_is_there(tmp_path: Path) -> None:
    """The whole point of the heartbeat: no owner must not cost a timeout."""
    broker, _, _ = _make(tmp_path)
    t0 = time.monotonic()
    assert broker.owner_alive()[0] is False
    assert time.monotonic() - t0 < 1.0


# --- open_request -----------------------------------------------------------


def test_open_request_writes_a_complete_record(tmp_path: Path) -> None:
    broker, spool, _ = _make(tmp_path, ttl_pad_s=30)
    assert broker.open_request(
        Q, question="Deploy?", options=["Yes", "No"], chat_id=42, timeout_s=120
    ) is True
    rec = spool.read(Q, "request")
    assert rec is not None
    assert rec["question"] == "Deploy?"
    assert rec["options"] == ["Yes", "No"]
    assert rec["chat_id"] == 42
    assert rec["timeout_s"] == 120
    assert rec["protocol"] == PROTOCOL_VERSION
    assert rec["requester_pid"] == os.getpid()
    # Padded past the caller deadline so a late answer still has somewhere to land.
    assert rec["expires_at"] > rec["created_at"] + 120


def test_open_request_twice_with_same_id_fails(tmp_path: Path) -> None:
    broker, _, _ = _make(tmp_path)
    assert broker.open_request(Q, question="a", options=None, chat_id=1, timeout_s=5) is True
    assert broker.open_request(Q, question="b", options=None, chat_id=1, timeout_s=5) is False


def test_open_request_returns_false_when_spool_unwritable(tmp_path: Path) -> None:
    blocker = tmp_path / "spool"
    blocker.write_text("not a dir", encoding="utf-8")
    broker = AskBroker(
        Spool(blocker, "asks"),
        tmp_path / ".lock",
        poll_interval_s=0.01,
        ttl_pad_s=30,
        heartbeat_stale_s=90,
    )
    assert broker.open_request(Q, question="a", options=None, chat_id=1, timeout_s=5) is False


# --- wait_for_answer --------------------------------------------------------


async def test_wait_for_answer_returns_a_trusted_answer(tmp_path: Path) -> None:
    broker, spool, _ = _make(tmp_path)
    _answer(spool, value="Yes", pid=os.getpid())
    got = await broker.wait_for_answer(Q, timeout_s=1)
    assert got is not None and got["value"] == "Yes"


async def test_wait_for_answer_times_out_to_none(tmp_path: Path) -> None:
    broker, _, _ = _make(tmp_path)
    t0 = time.monotonic()
    assert await broker.wait_for_answer(Q, timeout_s=0.2) is None
    assert time.monotonic() - t0 >= 0.2


async def test_wait_for_answer_picks_up_a_late_write(tmp_path: Path) -> None:
    broker, spool, _ = _make(tmp_path, poll_interval_s=0.05)

    async def answer_later() -> None:
        await asyncio.sleep(0.1)
        _answer(spool, value="Later", pid=os.getpid())

    task = asyncio.create_task(answer_later())
    got = await broker.wait_for_answer(Q, timeout_s=2)
    await task
    assert got is not None and got["value"] == "Later"


async def test_answer_written_during_the_final_interval_is_not_lost(tmp_path: Path) -> None:
    """Without a read after the loop, an answer landing in the last poll window
    is missed and then unlinked by close_request — silently swallowing what the
    operator typed."""
    broker, spool, _ = _make(tmp_path, poll_interval_s=10)
    # Deadline already passed: the loop body runs zero times, so only the
    # post-loop read can find this.
    _answer(spool, value="Squeaked in", pid=os.getpid())
    got = await broker.wait_for_answer(Q, timeout_s=0)
    assert got is not None and got["value"] == "Squeaked in"


# --- provenance -------------------------------------------------------------


async def test_answer_without_pid_is_rejected(tmp_path: Path) -> None:
    broker, spool, _ = _make(tmp_path)
    _answer(spool, value="Approve", pid=None)
    assert await broker.wait_for_answer(Q, timeout_s=0.1) is None


async def test_answer_from_wrong_pid_is_rejected(tmp_path: Path) -> None:
    """A forged answer must not be able to approve on the operator's behalf."""
    broker, spool, _ = _make(tmp_path)
    _answer(spool, value="Approve", pid=os.getpid() + 12345)
    assert await broker.wait_for_answer(Q, timeout_s=0.1) is None


async def test_answer_rejected_when_lock_file_is_missing(tmp_path: Path) -> None:
    broker, spool, lock_path = _make(tmp_path)
    lock_path.unlink()
    _answer(spool, value="Approve", pid=os.getpid())
    assert await broker.wait_for_answer(Q, timeout_s=0.1) is None


async def test_answer_accepted_once_ownership_settles(tmp_path: Path) -> None:
    """A rejected answer is not a permanent verdict: the request stays in the
    spool, so a genuine new owner can still serve it."""
    broker, spool, lock_path = _make(tmp_path, poll_interval_s=0.02)
    _answer(spool, value="Yes", pid=999_999)

    async def hand_over() -> None:
        await asyncio.sleep(0.05)
        lock_path.write_text("999999", encoding="utf-8")

    task = asyncio.create_task(hand_over())
    got = await broker.wait_for_answer(Q, timeout_s=2)
    await task
    assert got is not None and got["value"] == "Yes"


def test_rejected_answer_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    broker, spool, _ = _make(tmp_path)
    _answer(spool, value="Approve", pid=os.getpid() + 999)
    with caplog.at_level("WARNING", logger="mcgram.ask_broker"):
        assert broker._read_trusted_answer(Q) is None
    assert any("poll lock is held by" in r.getMessage() for r in caplog.records)


# --- close_request ----------------------------------------------------------


def test_close_request_removes_request_and_answer_but_keeps_the_claim(tmp_path: Path) -> None:
    """The claim is what stops an owner serving the request again; it expires
    with the request and the sweep removes it."""
    broker, spool, _ = _make(tmp_path)
    broker.open_request(Q, question="a", options=None, chat_id=1, timeout_s=5)
    spool.write_exclusive(Q, "taken", {"expires_at": time.time() + 60})
    _answer(spool, pid=os.getpid())
    broker.close_request(Q)
    assert spool.keys("request") == set()
    assert spool.keys("answer") == set()
    assert spool.keys("taken") == {Q}


def test_close_request_is_idempotent(tmp_path: Path) -> None:
    broker, _, _ = _make(tmp_path)
    broker.close_request(Q)
    broker.close_request(Q)
