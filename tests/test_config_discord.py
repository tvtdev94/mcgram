"""Settings + dispatch tests for the Discord transport."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from mcgram import dispatch
from mcgram import server as server_module
from mcgram.audit import AuditLog
from mcgram.config import DEFAULT_CHANNEL, Settings
from mcgram.errors import ConfigError
from mcgram.rate_limiter import RateLimiter
from mcgram.runtime import AppState
from mcgram.update_dispatcher import UpdateDispatcher

WEBHOOK_EVE = "https://discord.com/api/webhooks/111/eve-token"
WEBHOOK_DEPLOY = "https://discord.com/api/webhooks/222/deploy-token"


def _write(p: Path, body: str) -> Settings:
    p.write_text(body, encoding="utf-8")
    return Settings.load(p)


# --------------------------------------------------------------------------- #
# Config resolution
# --------------------------------------------------------------------------- #


def test_two_discord_channels_resolve_distinct_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCGRAM_DISCORD_WEBHOOK_EVE", WEBHOOK_EVE)
    monkeypatch.setenv("MCGRAM_DISCORD_WEBHOOK_DEPLOY", WEBHOOK_DEPLOY)
    s = _write(
        tmp_path / "c.yaml",
        """
discord:
  username: Tuan Assistant
channels:
  eve:
    transport: discord
    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_EVE
  deploy:
    transport: discord
    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_DEPLOY
""",
    )
    eve = s.resolve_destination("eve")
    deploy = s.resolve_destination("deploy")
    assert eve.transport == "discord"
    assert eve.discord_webhook_url == WEBHOOK_EVE
    assert eve.discord_username == "Tuan Assistant"
    assert deploy.discord_webhook_url == WEBHOOK_DEPLOY


def test_env_var_unset_names_the_variable_not_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MCGRAM_DISCORD_WEBHOOK_EVE", raising=False)
    s = _write(
        tmp_path / "c.yaml",
        """
channels:
  eve:
    transport: discord
    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_EVE
""",
    )
    with pytest.raises(ConfigError) as exc:
        s.resolve_destination("eve")
    msg = str(exc.value)
    assert "MCGRAM_DISCORD_WEBHOOK_EVE" in msg
    assert "not set" in msg


def test_discord_channel_missing_env_field_rejected_at_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="discord_webhook_env"):
        _write(
            tmp_path / "c.yaml",
            """
channels:
  eve:
    transport: discord
""",
        )


def test_unknown_channel_lists_valid_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCGRAM_DISCORD_WEBHOOK_EVE", WEBHOOK_EVE)
    s = _write(
        tmp_path / "c.yaml",
        """
channels:
  eve:
    transport: discord
    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_EVE
""",
    )
    with pytest.raises(ConfigError, match="unknown channel"):
        s.resolve_destination("nope")


def test_default_avatar_url_applied_when_section_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A channel may exist without a `discord:` section — display uses defaults."""
    monkeypatch.setenv("MCGRAM_DISCORD_WEBHOOK_EVE", WEBHOOK_EVE)
    s = _write(
        tmp_path / "c.yaml",
        """
channels:
  eve:
    transport: discord
    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_EVE
""",
    )
    dest = s.resolve_destination("eve")
    assert dest.discord_username == "mcgram"
    assert dest.discord_avatar_url is None


def test_invalid_avatar_url_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="avatar_url"):
        _write(
            tmp_path / "c.yaml",
            """
discord:
  avatar_url: not-a-url
channels:
  eve:
    transport: discord
    discord_webhook_env: X
""",
        )


# --------------------------------------------------------------------------- #
# Default-channel seeding
# --------------------------------------------------------------------------- #


def test_discord_only_single_channel_seeds_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCGRAM_DISCORD_WEBHOOK_EVE", WEBHOOK_EVE)
    s = _write(
        tmp_path / "c.yaml",
        """
channels:
  eve:
    transport: discord
    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_EVE
""",
    )
    assert DEFAULT_CHANNEL in s.channels
    dest = s.resolve_destination()
    assert dest.transport == "discord"
    assert dest.discord_webhook_url == WEBHOOK_EVE


def test_discord_only_multiple_channels_no_default_seed(tmp_path: Path) -> None:
    s = _write(
        tmp_path / "c.yaml",
        """
channels:
  eve:
    transport: discord
    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_EVE
  deploy:
    transport: discord
    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_DEPLOY
""",
    )
    assert DEFAULT_CHANNEL not in s.channels
    with pytest.raises(ConfigError, match="unknown channel"):
        s.resolve_destination()


def test_no_transport_at_all_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="bot.*ntfy.*Discord"):
        _write(tmp_path / "c.yaml", "defaults:\n  ask_timeout_s: 30\n")


# --------------------------------------------------------------------------- #
# Regression: telegram/ntfy behavior unchanged
# --------------------------------------------------------------------------- #


def test_regression_telegram_ntfy_default_unchanged(tmp_path: Path) -> None:
    """The canonical telegram+ntfy config still seeds default → telegram."""
    s = _write(
        tmp_path / "c.yaml",
        """
bot:
  operator_chat_id: 5
ntfy:
  default_topic: mcgram-h
""",
    )
    dest = s.resolve_destination()
    assert dest.transport == "telegram"
    assert dest.chat_id == 5


def test_regression_ntfy_only_default_unchanged(tmp_path: Path) -> None:
    s = _write(tmp_path / "c.yaml", "ntfy:\n  default_topic: mcgram-x\n")
    dest = s.resolve_destination()
    assert dest.transport == "ntfy"
    assert dest.ntfy_topic == "mcgram-x"


# --------------------------------------------------------------------------- #
# Poll lock: discord-only must not build a Telegram client / poller
# --------------------------------------------------------------------------- #


def test_discord_only_builds_no_telegram_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCGRAM_DISCORD_WEBHOOK_EVE", WEBHOOK_EVE)
    s = _write(
        tmp_path / "c.yaml",
        """
channels:
  eve:
    transport: discord
    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_EVE
""",
    )

    async def _exercise() -> tuple:
        async with server_module._build_clients(s) as triple:
            return triple

    tg, nt, dc = asyncio.run(_exercise())
    # No telegram client → no getUpdates poller → no poll lock contention.
    assert tg is None
    assert nt is None
    assert dc is not None


# --------------------------------------------------------------------------- #
# Dispatch: thread_id travels as a per-call arg
# --------------------------------------------------------------------------- #


class _FakeDiscordClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_message(self, webhook_url: str, content: str, **kwargs: Any) -> dict:
        self.calls.append({"webhook_url": webhook_url, "content": content, **kwargs})
        return {"id": "999"}


def _discord_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    monkeypatch.setenv("MCGRAM_DISCORD_WEBHOOK_EVE", WEBHOOK_EVE)
    s = _write(
        tmp_path / "c.yaml",
        """
channels:
  eve:
    transport: discord
    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_EVE
""",
    )
    fake = _FakeDiscordClient()
    state = AppState(
        settings=s,
        dispatcher=UpdateDispatcher(),
        rate=RateLimiter(20),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        discord_client=fake,  # type: ignore[arg-type]
    )
    return state, fake, s


def test_dispatch_forwards_thread_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, fake, s = _discord_state(tmp_path, monkeypatch)
    dest = s.resolve_destination("eve")
    asyncio.run(dispatch.send_text(state, dest, "hi", thread_id="1532"))
    assert fake.calls[0]["thread_id"] == "1532"
    assert fake.calls[0]["webhook_url"] == WEBHOOK_EVE


def test_dispatch_thread_id_none_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, fake, s = _discord_state(tmp_path, monkeypatch)
    dest = s.resolve_destination("eve")
    asyncio.run(dispatch.send_text(state, dest, "hi"))
    assert fake.calls[0]["thread_id"] is None


def test_dispatch_discord_without_client_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _fake, s = _discord_state(tmp_path, monkeypatch)
    state.discord_client = None
    dest = s.resolve_destination("eve")
    with pytest.raises(ConfigError, match="discord transport"):
        asyncio.run(dispatch.send_text(state, dest, "hi"))
