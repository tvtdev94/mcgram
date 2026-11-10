"""Discord tool-layer tests: thread_id surfacing, per-transport caps, audit safety."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mcgram.audit import AuditLog
from mcgram.config import Settings
from mcgram.discord_client import DiscordClient
from mcgram.rate_limiter import RateLimiter
from mcgram.runtime import AppState
from mcgram.tools import ask, edit_message, send_file, send_message
from mcgram.update_dispatcher import UpdateDispatcher

# Fake webhook: id 123456789, token tok_secret_abc. The token must NEVER be logged.
WEBHOOK = "https://discord.com/api/webhooks/123456789/tok_secret_abc"
TG_SEND = "https://api.telegram.org/botfake-token/sendMessage"


def _audit(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.fixture
async def discord_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AppState]:
    monkeypatch.setenv("MCGRAM_DISCORD_WEBHOOK_EVE", WEBHOOK)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
channels:
  eve:
    transport: discord
    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_EVE
""",
        encoding="utf-8",
    )
    settings = Settings.load(cfg)
    async with DiscordClient() as dc:
        yield AppState(
            settings=settings,
            dispatcher=UpdateDispatcher(),
            rate=RateLimiter(100),
            audit=AuditLog(tmp_path / "audit.jsonl"),
            discord_client=dc,
        )


# --------------------------------------------------------------------------- #
# thread_id surfacing
# --------------------------------------------------------------------------- #


async def test_send_message_into_thread(discord_state: AppState, httpx_mock) -> None:
    httpx_mock.add_response(method="POST", json={"id": "777"})
    out = await send_message.handle(
        discord_state, text="hi", channel="eve", thread_id="1532959062499659987"
    )
    assert out["ok"] is True
    assert out["transport"] == "discord"
    req = httpx_mock.get_requests()[0]
    assert req.url.params.get("thread_id") == "1532959062499659987"


async def test_send_message_base_channel_no_thread(
    discord_state: AppState, httpx_mock
) -> None:
    httpx_mock.add_response(method="POST", json={"id": "777"})
    await send_message.handle(discord_state, text="hi", channel="eve")
    assert "thread_id" not in httpx_mock.get_requests()[0].url.params


async def test_schema_exposes_thread_id() -> None:
    props = send_message.schema()["inputSchema"]["properties"]
    assert props["thread_id"]["type"] == "string"


# --------------------------------------------------------------------------- #
# Per-transport text cap
# --------------------------------------------------------------------------- #


def _payload_json(req) -> dict:
    """The `payload_json` form field of a multipart webhook upload."""
    body = req.content
    start = body.index(b'name="payload_json"')
    start = body.index(b"\r\n\r\n", start) + 4
    return json.loads(body[start: body.index(b"\r\n--", start)])


async def test_discord_text_over_2000_sent_as_file(
    discord_state: AppState, httpx_mock
) -> None:
    httpx_mock.add_response(method="POST", json={"id": "901"})
    out = await send_message.handle(discord_state, text="x" * 2001, channel="eve")
    assert out["ok"] is True
    assert out["delivered_as"] == "file"
    assert out["message_id"] == "901"
    req = httpx_mock.get_requests()[0]
    assert b'name="files[0]"; filename="message.txt"' in req.content
    assert _payload_json(req)["content"] == "x" * 1023 + "…"


async def test_telegram_text_2001_still_allowed(app_state: AppState, httpx_mock) -> None:
    httpx_mock.add_response(url=TG_SEND, json={"ok": True, "result": {"message_id": 1}})
    out = await send_message.handle(app_state, text="x" * 2001)
    assert out["ok"] is True


async def test_unknown_channel_wins_over_too_long(discord_state: AppState) -> None:
    out = await send_message.handle(discord_state, text="x" * 5000, channel="nope")
    assert out["reason"] == "unknown_channel"


# --------------------------------------------------------------------------- #
# thread_id on a non-discord transport → ignored with a note
# --------------------------------------------------------------------------- #


async def test_thread_id_ignored_note_on_telegram(app_state: AppState, httpx_mock) -> None:
    httpx_mock.add_response(url=TG_SEND, json={"ok": True, "result": {"message_id": 1}})
    out = await send_message.handle(app_state, text="hi", thread_id="123")
    assert out["ok"] is True
    assert "note" in out
    assert "ignored" in out["note"]


# --------------------------------------------------------------------------- #
# ask is unsupported on discord (one-way)
# --------------------------------------------------------------------------- #


async def test_ask_on_discord_returns_unsupported(discord_state: AppState) -> None:
    out = await ask.handle(discord_state, question="ok?", channel="eve")
    assert out["error"] == "transport_unsupported"
    rec = _audit(discord_state.audit.path)[-1]
    assert rec["reason"] == "transport_unsupported"
    assert rec["transport"] == "discord"


# --------------------------------------------------------------------------- #
# File size cap + thread on file
# --------------------------------------------------------------------------- #


async def test_file_over_discord_cap_rejected_before_network(
    discord_state: AppState, tmp_path: Path
) -> None:
    discord_state.settings.allow_outside_cwd = True
    discord_state.settings.limits.discord_file_max_bytes = 10
    p = tmp_path / "big.bin"
    p.write_bytes(b"x" * 100)
    out = await send_file.handle(discord_state, path=str(p), channel="eve")
    assert out["error"] == "rejected"
    assert out["reason"] == "file_too_large"


async def test_send_file_into_thread(
    discord_state: AppState, httpx_mock, tmp_path: Path
) -> None:
    discord_state.settings.allow_outside_cwd = True
    p = tmp_path / "log.txt"
    p.write_bytes(b"data")
    httpx_mock.add_response(method="POST", json={"id": "777"})
    out = await send_file.handle(
        discord_state, path=str(p), channel="eve", thread_id="42", caption="c"
    )
    assert out["ok"] is True
    assert httpx_mock.get_requests()[0].url.params.get("thread_id") == "42"


# --------------------------------------------------------------------------- #
# Audit safety — never log the webhook token
# --------------------------------------------------------------------------- #


async def test_audit_logs_webhook_id_not_token(
    discord_state: AppState, httpx_mock
) -> None:
    httpx_mock.add_response(method="POST", json={"id": "777"})
    await send_message.handle(
        discord_state, text="secret deploy", channel="eve", thread_id="1532"
    )
    raw = Path(discord_state.audit.path).read_text(encoding="utf-8")
    assert "tok_secret_abc" not in raw  # token must never appear
    assert WEBHOOK not in raw  # nor the full URL
    rec = _audit(discord_state.audit.path)[-1]
    assert rec["discord_webhook_id"] == "123456789"
    assert rec["thread_id"] == "1532"


async def test_telegram_audit_record_has_no_discord_fields(
    app_state: AppState, httpx_mock
) -> None:
    httpx_mock.add_response(url=TG_SEND, json={"ok": True, "result": {"message_id": 9}})
    await send_message.handle(app_state, text="hi")
    rec = _audit(app_state.audit.path)[-1]
    assert "discord_webhook_id" not in rec
    assert "thread_id" not in rec


# --------------------------------------------------------------------------- #
# Mentions
# --------------------------------------------------------------------------- #

ALICE_ID = "123456789012345678"


@pytest.fixture
async def discord_mention_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AppState]:
    monkeypatch.setenv("MCGRAM_DISCORD_WEBHOOK_EVE", WEBHOOK)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
discord:
  mentions:
    alice: "{ALICE_ID}"
channels:
  eve:
    transport: discord
    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_EVE
""",
        encoding="utf-8",
    )
    settings = Settings.load(cfg)
    async with DiscordClient() as dc:
        yield AppState(
            settings=settings,
            dispatcher=UpdateDispatcher(),
            rate=RateLimiter(100),
            audit=AuditLog(tmp_path / "audit.jsonl"),
            discord_client=dc,
        )


async def test_mention_prepends_token_and_whitelists_user(
    discord_mention_state: AppState, httpx_mock
) -> None:
    httpx_mock.add_response(method="POST", json={"id": "777"})
    out = await send_message.handle(
        discord_mention_state, text="deploy done", channel="eve", mention=["alice"]
    )
    assert out["ok"] is True
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["content"] == f"<@{ALICE_ID}> deploy done"
    assert body["allowed_mentions"] == {"parse": [], "users": [ALICE_ID]}


async def test_unknown_mention_rejected_before_network(
    discord_mention_state: AppState, httpx_mock
) -> None:
    out = await send_message.handle(
        discord_mention_state, text="hi", channel="eve", mention=["ghost"]
    )
    assert out["error"] == "invalid_input"
    assert out["reason"] == "unknown_mention"
    assert out["unknown"] == ["ghost"]
    assert out["known"] == ["alice"]
    assert httpx_mock.get_requests() == []  # never contacted Discord
    rec = _audit(discord_mention_state.audit.path)[-1]
    assert rec["reason"] == "unknown_mention"


async def test_no_mention_still_sets_empty_allowed_mentions(
    discord_mention_state: AppState, httpx_mock
) -> None:
    httpx_mock.add_response(method="POST", json={"id": "777"})
    await send_message.handle(
        discord_mention_state, text="hi @everyone", channel="eve"
    )
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["allowed_mentions"] == {"parse": []}


async def test_mention_ignored_note_on_telegram(
    app_state: AppState, httpx_mock
) -> None:
    httpx_mock.add_response(url=TG_SEND, json={"ok": True, "result": {"message_id": 1}})
    out = await send_message.handle(app_state, text="hi", mention=["alice"])
    assert out["ok"] is True
    assert "mention ignored" in out["note"]


async def test_mention_prefix_counts_toward_2000_cap(
    discord_mention_state: AppState, httpx_mock
) -> None:
    # 1995 chars + "<@id> " prefix (> 5) exceeds Discord's 2000-char content cap,
    # so it goes out as a file; the caption still pings alice.
    httpx_mock.add_response(method="POST", json={"id": "902"})
    out = await send_message.handle(
        discord_mention_state, text="x" * 1995, channel="eve", mention=["alice"]
    )
    assert out["delivered_as"] == "file"
    payload = _payload_json(httpx_mock.get_requests()[0])
    assert payload["content"].startswith(f"<@{ALICE_ID}> xxx")
    assert len(payload["content"]) == 1024
    assert payload["allowed_mentions"] == {"parse": [], "users": [ALICE_ID]}


# --------------------------------------------------------------------------- #
# edit_message and inline images on Discord
# --------------------------------------------------------------------------- #

async def test_edit_message_in_thread(discord_state: AppState, httpx_mock) -> None:
    httpx_mock.add_response(method="PATCH", json={"id": "555"})
    out = await edit_message.handle(
        discord_state, message_id="555", text="80% done", channel="eve", thread_id="42",
    )
    assert out == {"ok": True, "message_id": "555", "channel": "eve", "transport": "discord"}
    req = httpx_mock.get_requests()[0]
    assert req.url.path.endswith("/123456789/tok_secret_abc/messages/555")
    assert req.url.params.get("thread_id") == "42"
    rec = _audit(discord_state.audit.path)[-1]
    assert rec["discord_webhook_id"] == "123456789"
    assert "tok_secret_abc" not in json.dumps(rec)


async def test_edit_message_unknown_message_explained(
    discord_state: AppState, httpx_mock
) -> None:
    httpx_mock.add_response(
        method="PATCH", status_code=404, json={"message": "Unknown Message", "code": 10008},
    )
    out = await edit_message.handle(discord_state, message_id="1", text="x", channel="eve")
    assert out["error"] == "discord_api"
    assert "message not found" in out["reason"]


async def test_edit_message_over_2000_rejected(discord_state: AppState, httpx_mock) -> None:
    out = await edit_message.handle(discord_state, message_id="1", text="x" * 2001,
                                    channel="eve")
    assert out["reason"] == "text_too_long"
    assert out["max"] == 2000
    assert httpx_mock.get_requests() == []


@pytest.mark.parametrize(
    ("name", "mime", "as_document", "sent_as"),
    [
        ("chart.png", b"image/png", False, "photo"),
        ("loop.gif", b"image/gif", False, "animation"),
        ("chart.png", b"image/png", True, "document"),
    ],
    ids=["png", "gif", "png-as-document"],
)
async def test_image_attachment_reports_how_it_was_sent(
    discord_state: AppState, httpx_mock, tmp_path: Path,
    name: str, mime: bytes, as_document: bool, sent_as: str,
) -> None:
    """`sent_as` uses the words Telegram would report for the same request."""
    discord_state.settings.allow_outside_cwd = True
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\x00")
    httpx_mock.add_response(method="POST", json={"id": "903"})
    out = await send_file.handle(discord_state, path=str(p), channel="eve",
                                 as_document=as_document)
    assert out["sent_as"] == sent_as
    # Discord previews an attachment inline from its image content type.
    body = httpx_mock.get_requests()[0].content
    assert b'filename="' + name.encode() + b'"\r\nContent-Type: ' + mime in body


async def test_send_file_mention_prepends_caption(
    discord_mention_state: AppState, httpx_mock, tmp_path: Path
) -> None:
    discord_mention_state.settings.allow_outside_cwd = True
    p = tmp_path / "log.txt"
    p.write_bytes(b"data")
    httpx_mock.add_response(method="POST", json={"id": "777"})
    out = await send_file.handle(
        discord_mention_state, path=str(p), channel="eve",
        caption="build log", mention=["alice"],
    )
    assert out["ok"] is True
    req = httpx_mock.get_requests()[0]
    assert f"<@{ALICE_ID}> build log".encode() in req.content
    assert b'"users": ["' + ALICE_ID.encode() + b'"]' in req.content


async def test_mention_names_audited(
    discord_mention_state: AppState, httpx_mock
) -> None:
    httpx_mock.add_response(method="POST", json={"id": "777"})
    await send_message.handle(
        discord_mention_state, text="hi", channel="eve", mention=["alice"]
    )
    rec = _audit(discord_mention_state.audit.path)[-1]
    assert rec["mentions"] == ["alice"]


# --------------------------------------------------------------------------- #
# reminders over discord
# --------------------------------------------------------------------------- #


async def test_reminder_fires_on_a_discord_channel(
    discord_state: AppState, httpx_mock
) -> None:
    """set_reminder returned success on discord while delivery never happened.

    The scheduler carried its own telegram/ntfy clients and had no discord
    branch, so the fire path died on an assertion after the caller had already
    been told the reminder was scheduled.
    """
    import asyncio

    from mcgram.reminders import ReminderScheduler

    httpx_mock.add_response(method="POST", json={"id": "777"})
    sched = ReminderScheduler(discord_state)
    out = sched.create("check logs", delay_s=1, channel="eve")
    assert out["transport"] == "discord"
    for _ in range(30):
        if httpx_mock.get_requests():
            break
        await asyncio.sleep(0.1)
    req = httpx_mock.get_requests()
    assert req, "reminder never reached the discord webhook"
    assert b"check logs" in req[0].content
    fired = [r for r in _audit(discord_state.audit.path) if r["tool"] == "_reminder_fire"]
    assert fired and fired[-1]["status"] == "ok"
    sched.shutdown()


async def test_discord_webhook_token_never_leaks_on_network_failure(
    discord_state: AppState, httpx_mock
) -> None:
    import httpx

    httpx_mock.add_exception(httpx.ConnectError(f"cannot connect to {WEBHOOK}"))
    out = await send_message.handle(discord_state, text="hi", channel="eve")
    assert out["error"] == "transport_unreachable"
    assert "tok_secret_abc" not in json.dumps(out)
    assert "tok_secret_abc" not in Path(discord_state.audit.path).read_text(
        encoding="utf-8"
    )
