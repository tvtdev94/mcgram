"""End-to-end Discord path: multi-channel routing, thread errors, ask, no token leak."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mcgram.audit import AuditLog
from mcgram.config import Settings
from mcgram.discord_client import DiscordClient
from mcgram.rate_limiter import RateLimiter
from mcgram.runtime import AppState
from mcgram.tools import ask, send_message
from mcgram.update_dispatcher import UpdateDispatcher

WEBHOOK_EVE = "https://discord.com/api/webhooks/111/eve-token"
WEBHOOK_DEPLOY = "https://discord.com/api/webhooks/222/deploy-token"


@pytest.fixture
async def two_channel_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AppState]:
    monkeypatch.setenv("MCGRAM_DISCORD_WEBHOOK_EVE", WEBHOOK_EVE)
    monkeypatch.setenv("MCGRAM_DISCORD_WEBHOOK_DEPLOY", WEBHOOK_DEPLOY)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
discord:
  username: mcgram
channels:
  eve:
    transport: discord
    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_EVE
  deploy:
    transport: discord
    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_DEPLOY
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


async def test_two_channels_route_to_distinct_webhooks(
    two_channel_state: AppState, httpx_mock
) -> None:
    httpx_mock.add_response(method="POST", json={"id": "1"})
    httpx_mock.add_response(method="POST", json={"id": "2"})
    r1 = await send_message.handle(two_channel_state, text="a", channel="eve")
    r2 = await send_message.handle(two_channel_state, text="b", channel="deploy")
    assert r1["ok"] and r2["ok"]
    urls = [str(r.url) for r in httpx_mock.get_requests()]
    assert any("webhooks/111/eve-token" in u for u in urls)
    assert any("webhooks/222/deploy-token" in u for u in urls)


async def test_thread_id_reflected_in_query(
    two_channel_state: AppState, httpx_mock
) -> None:
    httpx_mock.add_response(method="POST", json={"id": "1"})
    httpx_mock.add_response(method="POST", json={"id": "2"})
    await send_message.handle(two_channel_state, text="a", channel="eve", thread_id="42")
    await send_message.handle(two_channel_state, text="b", channel="eve")
    reqs = httpx_mock.get_requests()
    assert reqs[0].url.params.get("thread_id") == "42"
    assert "thread_id" not in reqs[1].url.params


async def test_bad_thread_id_is_structured_error_not_crash(
    two_channel_state: AppState, httpx_mock
) -> None:
    httpx_mock.add_response(
        method="POST", status_code=400,
        json={"message": "Unknown Channel", "code": 10003},
    )
    out = await send_message.handle(
        two_channel_state, text="x", channel="eve", thread_id="bad"
    )
    assert out["error"] == "discord_api"
    assert "thread_id" in out["reason"]
    # The server keeps running: a subsequent call still works.
    httpx_mock.add_response(method="POST", json={"id": "ok"})
    ok = await send_message.handle(two_channel_state, text="y", channel="deploy")
    assert ok["ok"] is True


async def test_ask_on_discord_channel_unsupported(two_channel_state: AppState) -> None:
    out = await ask.handle(two_channel_state, question="Deploy?", channel="eve")
    assert out["error"] == "transport_unsupported"


async def test_no_webhook_token_or_url_in_audit(
    two_channel_state: AppState, httpx_mock
) -> None:
    httpx_mock.add_response(method="POST", json={"id": "1"})
    await send_message.handle(
        two_channel_state, text="deploy the secret build", channel="eve", thread_id="999"
    )
    raw = Path(two_channel_state.audit.path).read_text(encoding="utf-8")
    assert "eve-token" not in raw       # token segment
    assert WEBHOOK_EVE not in raw       # full URL
    assert "111" in raw                 # webhook ID is fine to log
