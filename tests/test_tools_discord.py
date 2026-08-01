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
from mcgram.tools import ask, send_file, send_message
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


async def test_discord_text_over_2000_rejected(discord_state: AppState) -> None:
    out = await send_message.handle(discord_state, text="x" * 2001, channel="eve")
    assert out["error"] == "invalid_input"
    assert out["reason"] == "text_too_long"
    assert out["max"] == 2000
    assert out["transport"] == "discord"


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
