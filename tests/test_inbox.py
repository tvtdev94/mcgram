"""Operator inbox, intake side: what the polling session stores, and its supervisor.

Updates go through the real `poll_loop` and the real dispatcher into the intake
handler that `start_inbox` registers, against a real `Spool` in `tmp_path`.
Only `getUpdates` is faked. What a reader gets back (signature, label and age
filters) is owned by the `check_inbox` tool tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from mcgram.audit import AuditLog
from mcgram.config import Settings
from mcgram.inbox import (
    DROP_REACTION,
    HEARTBEAT_KEY,
    HEARTBEAT_KIND,
    MAX_PENDING,
    Command,
    InboxStore,
    signing_key,
)
from mcgram.inbox_wiring import INBOX_NAMESPACE, session_label, start_inbox
from mcgram.poll_ownership import cancel_task
from mcgram.polling import poll_loop
from mcgram.rate_limiter import RateLimiter
from mcgram.runtime import AppState
from mcgram.spool import Spool
from mcgram.update_dispatcher import UpdateDispatcher
from tests.test_ask_server import FakeClient as SendClient
from tests.test_polling import FakeClient as PollClient

OPERATOR = 42


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_TOKEN", "fake-token")


def _settings(tmp_path: Path, *, enabled: bool = True, operator: int = OPERATOR) -> Settings:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
bot:
  token_env: TEST_TOKEN
  operator_chat_id: {operator}
audit:
  path: {(tmp_path / 'audit.jsonl').as_posix()}
broker:
  spool_dir: {(tmp_path / 'spool').as_posix()}
  poll_interval_s: 0.02
  sweep_interval_s: 1
inbox:
  enabled: {str(enabled).lower()}
""",
        encoding="utf-8",
    )
    return Settings.load(cfg)


class ReactClient(SendClient):
    """Also records reactions; the request body is owned by the check_inbox tests."""

    def __init__(self) -> None:
        super().__init__()
        self.reactions: list[tuple[int, int, str]] = []

    async def set_message_reaction(self, chat_id: int, message_id: int, emoji: str,
                                   **kwargs: Any) -> bool:
        self.reactions.append((chat_id, message_id, emoji))
        return True


def _state(settings: Settings, *, rate_per_min: int = 20) -> AppState:
    return AppState(
        settings=settings,
        dispatcher=UpdateDispatcher(),
        rate=RateLimiter(rate_per_min),
        audit=AuditLog(settings.audit.path),
        client=ReactClient(),  # type: ignore[arg-type]
    )


async def _reactions(state: AppState) -> list[tuple[int, int, str]]:
    """Reactions sent so far, after letting the background senders run."""
    await asyncio.sleep(0.05)
    return state.client.reactions  # type: ignore[union-attr]


def _drop_reaction(update_id: int) -> tuple[int, int, str]:
    return (OPERATOR, 100 + update_id, DROP_REACTION)


def _msg(update_id: int = 1, text: str | None = "/stop", **fields: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": 100 + update_id,
        "date": int(time.time()),
        "chat": {"id": OPERATOR, "type": "private"},
        "from": {"id": OPERATOR, "is_bot": False, "first_name": "Op"},
        "text": text,
    }
    message.update(fields)
    if text is None:
        del message["text"]
    return {"update_id": update_id, "message": message}


def _inbox_audit(settings: Settings) -> list[dict[str, Any]]:
    path = Path(settings.audit.path)
    if not path.exists():
        return []
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if r.get("tool") == "_inbox"]


def _records(settings: Settings) -> list[str]:
    folder = Spool(settings.spool_root, INBOX_NAMESPACE).dir
    if not folder.exists():
        return []
    return sorted(n for n in os.listdir(folder) if n.endswith(".cmd.json"))


async def _until(predicate: Any, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def _poll(state: AppState, batches: list[list[dict[str, Any]]]) -> None:
    """Run the real poll loop until every batch has been dispatched."""
    client = PollClient(batches)  # type: ignore[arg-type]
    task = asyncio.create_task(poll_loop(
        client, state.dispatcher, OPERATOR, state.audit,  # type: ignore[arg-type]
        long_poll_timeout_s=1, initial_backoff_s=0.01,
    ))
    try:
        assert await _until(lambda: client.calls > len(batches))
    finally:
        await cancel_task(task)


@pytest.fixture
async def owner(tmp_path: Path):
    """An inbox-enabled state whose supervisor runs; it does not poll by itself."""
    state = _state(_settings(tmp_path))
    task = start_inbox(state)
    assert task is not None
    try:
        yield state
    finally:
        await cancel_task(task)


_EDITED = {"update_id": 1, "edited_message": _msg(text="/stop")["message"]}
_CALLBACK = {"update_id": 1, "callback_query": {
    "id": "cb", "data": "q_1:0", "message": {"chat": {"id": OPERATOR}}}}


@pytest.mark.parametrize(("update", "expected"), [
    pytest.param(_msg(text="/Note@Alpha  ship\u200b it\x07\ud800"), ("queued", None),
                 id="accepted"),
    pytest.param(_msg(chat={"id": OPERATOR, "type": "supergroup"}),
                 ("rejected", "not_private"), id="group-chat"),
    pytest.param(_msg(**{"from": {"id": 7, "is_bot": False}}),
                 ("rejected", "sender_mismatch"), id="other-sender"),
    pytest.param(_msg(**{"from": {"id": OPERATOR, "is_bot": True}}),
                 ("rejected", "sender_mismatch"), id="bot-sender"),
    pytest.param(_msg(via_bot={"id": 9, "is_bot": True}),
                 ("rejected", "sender_mismatch"), id="via-bot"),
    pytest.param(_msg(forward_origin={"type": "user", "date": 1}),
                 ("rejected", "forwarded"), id="forwarded"),
    pytest.param(_msg(forward_date=1), ("rejected", "forwarded"), id="forwarded-legacy"),
    pytest.param(_msg(is_automatic_forward=True), ("rejected", "forwarded"), id="auto-forward"),
    pytest.param(_EDITED, ("ignored", "edited"), id="edited"),
    pytest.param(_msg(text=None, caption="/stop"), ("ignored", "not_text"), id="caption-only"),
    pytest.param(_msg(text="please stop"), ("ignored", "not_command"), id="plain-text"),
    pytest.param(_msg(text="/start"), ("ignored", "reserved"), id="start"),
    pytest.param(_msg(text="/note " + "x" * 1001), ("dropped", "too_long"), id="too-long"),
    pytest.param(_msg(text="/deploy prod"), ("dropped", "unknown_command"), id="unknown-command"),
    pytest.param(_msg(text="/stop @api"), ("dropped", "ambiguous_target"), id="spaced-target"),
    pytest.param(_msg(date="yesterday"), ("rejected", "malformed"), id="malformed"),
    pytest.param(_CALLBACK, None, id="button-tap-is-silent"),
])
async def test_intake_filter(owner: AppState, update: dict[str, Any], expected: Any) -> None:
    await _poll(owner, [[update]])
    rows = _inbox_audit(owner.settings)
    if expected is None:
        assert rows == []
        assert _records(owner.settings) == []
        return
    status, reason = expected
    assert [(r["status"], r.get("reason"), r["update_id"]) for r in rows] == \
        [(status, reason, 1)]
    if status == "queued":
        assert _records(owner.settings) == ["u1.cmd.json"]
        assert (rows[0]["command"], rows[0]["target"], rows[0]["text"]) == \
            ("note", "alpha", "ship it")
    else:
        assert _records(owner.settings) == []
    # The operator learns about a dropped command; refusals of other senders stay silent.
    assert await _reactions(owner) == ([_drop_reaction(1)] if status == "dropped" else [])


async def test_redelivery_to_a_new_owner_is_stored_once(tmp_path: Path) -> None:
    """A new poll owner starts at offset 0 and gets unconfirmed updates again."""
    settings = _settings(tmp_path)
    first, second = _state(settings), _state(settings)
    tasks = [start_inbox(first), start_inbox(second)]
    try:
        await first.dispatcher.dispatch(_msg(update_id=5))
        await second.dispatcher.dispatch(_msg(update_id=5))
    finally:
        for task in tasks:
            await cancel_task(task)
    assert _records(settings) == ["u5.cmd.json"]
    assert [(r["status"], r.get("reason")) for r in _inbox_audit(settings)] == \
        [("queued", None), ("ignored", "duplicate")]
    assert await _reactions(second) == []


async def test_a_forged_file_cannot_occupy_a_command_slot(owner: AppState) -> None:
    """A planted file under the next update id must not swallow the real command."""
    spool = Spool(owner.settings.spool_root, INBOX_NAMESPACE)
    assert spool.write("u5", "cmd", {"expires_at": 1e12})
    await owner.dispatcher.dispatch(_msg(update_id=5, text="/stop"))
    [row] = _inbox_audit(owner.settings)
    assert (row["status"], row.get("replaced")) == ("queued", "bad_signature")
    found, forged = owner.inbox.store.unread(set(), "any", 0, time.time())
    assert ([r["command"] for r in found], forged) == (["stop"], [])


async def test_only_live_signed_records_count_toward_the_cap(owner: AppState) -> None:
    spool = Spool(owner.settings.spool_root, INBOX_NAMESPACE)
    for uid in range(200, 200 + MAX_PENDING):  # junk a local process could plant
        assert spool.write(f"u{uid}", "cmd", {"expires_at": time.time() + 600})
    store, old = owner.inbox.store, time.time() - 2 * owner.settings.inbox.ttl_s
    for uid in range(300, 300 + MAX_PENDING):  # genuine, but expired and not yet swept
        cmd = Command(update_id=uid, message_id=1, chat_id=OPERATOR, command="note",
                      target=None, args="", sent_at=int(old))
        assert store.publish(cmd, old) == "queued"
    await owner.dispatcher.dispatch(_msg(update_id=1))
    assert [r["status"] for r in _inbox_audit(owner.settings)] == ["queued"]


async def test_redelivery_stays_a_duplicate_when_telegram_runs_ahead(owner: AppState) -> None:
    """Our clock 100 s behind Telegram's: the record must outlive the window in
    which a redelivery of the same update still counts as fresh."""
    store = owner.inbox.store
    ttl = owner.settings.inbox.ttl_s
    local = time.time()
    cmd = Command(update_id=8, message_id=1, chat_id=OPERATOR, command="stop",
                  target=None, args="", sent_at=int(local) + 100)
    assert store.publish(cmd, local) == "queued"
    later = local + ttl + 50  # still within ttl of Telegram's date
    store.spool.sweep(later)
    assert store.publish(cmd, later) == "duplicate"


async def test_update_older_than_ttl_is_dropped(owner: AppState) -> None:
    old = _msg(date=int(time.time()) - owner.settings.inbox.ttl_s - 5)
    await owner.dispatcher.dispatch(old)
    assert _records(owner.settings) == []
    assert [(r["status"], r["reason"]) for r in _inbox_audit(owner.settings)] == \
        [("dropped", "stale")]
    assert await _reactions(owner) == [_drop_reaction(1)]


async def test_pending_cap_drops_the_excess(tmp_path: Path) -> None:
    state = _state(_settings(tmp_path), rate_per_min=600)
    task = start_inbox(state)
    try:
        for uid in range(1, MAX_PENDING + 2):
            await state.dispatcher.dispatch(_msg(update_id=uid))
    finally:
        await cancel_task(task)
    assert len(_records(state.settings)) == MAX_PENDING
    last = _inbox_audit(state.settings)[-1]
    assert (last["status"], last["reason"], last["update_id"]) == \
        ("dropped", "inbox_full", MAX_PENDING + 1)
    assert await _reactions(state) == [_drop_reaction(MAX_PENDING + 1)]


async def test_intake_is_rate_limited(owner: AppState) -> None:
    for uid in range(1, 22):
        await owner.dispatcher.dispatch(_msg(update_id=uid))
    assert len(_records(owner.settings)) == 20
    last = _inbox_audit(owner.settings)[-1]
    assert (last["status"], last["reason"], last["update_id"]) == \
        ("dropped", "rate_limited", 21)
    assert await _reactions(owner) == [_drop_reaction(21)]


async def test_heartbeat_follows_poll_ownership(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = _state(settings)
    beat = Spool(settings.spool_root, INBOX_NAMESPACE).path_for(HEARTBEAT_KEY, HEARTBEAT_KIND)
    assert beat is not None
    task = start_inbox(state)
    try:
        await asyncio.sleep(0.1)
        assert not beat.exists()  # a session that does not poll never beats
        state.owns_polling = True
        assert await _until(beat.exists)
        assert json.loads(beat.read_text(encoding="utf-8"))["pid"] == os.getpid()
        state.owns_polling = False  # demoted: readers must stop waiting at once
        assert await _until(lambda: not beat.exists())
        state.owns_polling = True
        assert await _until(beat.exists)
    finally:
        await cancel_task(task)
    assert not beat.exists()  # shutdown withdraws it too


async def test_non_owner_never_removes_the_owner_heartbeat(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    spool = Spool(settings.spool_root, INBOX_NAMESPACE)
    other = {"pid": os.getppid(), "at": time.time(), "protocol": 1,
             "expires_at": time.time() + 90}
    assert spool.write(HEARTBEAT_KEY, HEARTBEAT_KIND, other)
    task = start_inbox(_state(settings))  # boots, runs and shuts down as a non-owner
    await asyncio.sleep(0.1)
    await cancel_task(task)
    assert spool.read(HEARTBEAT_KEY, HEARTBEAT_KIND) == other


@pytest.mark.parametrize("expires_in", [-1, 10 * 86400], ids=["expired", "beyond-max-ttl"])
async def test_expired_records_are_swept_by_every_instance(
    tmp_path: Path, expires_in: float
) -> None:
    settings = _settings(tmp_path)
    spool = Spool(settings.spool_root, INBOX_NAMESPACE)
    assert spool.write("u7", "cmd", {"expires_at": time.time() + expires_in})
    task = start_inbox(_state(settings))  # not the owner
    try:
        assert await _until(lambda: _records(settings) == [])
    finally:
        await cancel_task(task)


async def test_group_operator_disables_intake(tmp_path: Path) -> None:
    settings = _settings(tmp_path, operator=-100123)
    state = _state(settings)
    assert start_inbox(state) is None
    assert state.inbox is not None
    assert "private" in state.inbox.unavailable
    group = _msg(chat={"id": -100123, "type": "group"})
    await state.dispatcher.dispatch(group)
    assert _inbox_audit(settings) == []  # no intake handler was registered
    assert not Spool(settings.spool_root, INBOX_NAMESPACE).dir.exists()


async def test_disabled_inbox_leaves_no_trace(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=False)
    state = _state(settings)
    assert start_inbox(state) is None
    assert state.inbox is None
    await state.dispatcher.dispatch(_msg())
    assert _inbox_audit(settings) == []
    assert not Spool(settings.spool_root, INBOX_NAMESPACE).dir.exists()


async def test_disabling_the_inbox_still_sweeps_old_records_at_startup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = InboxStore(Spool(settings.spool_root, INBOX_NAMESPACE), signing_key("fake-token"),
                       operator_chat_id=OPERATOR, ttl_s=60, heartbeat_stale_s=90)
    for uid, received in ((1, time.time() - 600), (2, time.time())):
        cmd = Command(update_id=uid, message_id=1, chat_id=OPERATOR, command="note",
                      target=None, args="secret", sent_at=int(received))
        assert store.publish(cmd, received) == "queued"
    assert start_inbox(_state(_settings(tmp_path, enabled=False))) is None
    assert _records(settings) == ["u2.cmd.json"]  # only the unexpired one is left


@pytest.mark.parametrize(("env", "folder", "expected"), [
    ("My-API.v2", "ignored", "my_api_v2"),
    ("", "Web App", "web_app"),
    (None, "x" * 40, "x" * 32),
    (None, "---", "___"),
    (None, "", "session"),
])
def test_session_label_is_normalized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    env: str | None, folder: str, expected: str,
) -> None:
    if env is None:
        monkeypatch.delenv("MCGRAM_SESSION_LABEL", raising=False)
    else:
        monkeypatch.setenv("MCGRAM_SESSION_LABEL", env)
    cwd = tmp_path / folder if folder else Path("/")
    cwd.mkdir(exist_ok=True)
    monkeypatch.chdir(cwd)
    assert session_label() == expected


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot remove the working folder")
def test_a_deleted_working_folder_falls_back_to_the_default_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MCGRAM_SESSION_LABEL", raising=False)
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    assert session_label() == "session"
