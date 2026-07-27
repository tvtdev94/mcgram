"""server.main() error UX tests."""

from __future__ import annotations

import asyncio

import pytest

from mcgram import server as server_module
from mcgram.errors import AuthError, ConfigError, LockHeldError


def test_lock_held_does_not_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A held poll lock must never reach `main()` as a fatal error.

    This is the -32000 bug: `main()` used to catch LockHeldError and exit(1),
    killing the MCP handshake for every Claude Code session after the first.
    Contention is now handled inside PollOwnership by degrading to send-only,
    so a LockHeldError escaping to here would be a real regression — let it
    surface loudly rather than turning it back into a silent exit.
    """
    async def boom() -> None:
        raise LockHeldError(12345, "/tmp/.lock")

    monkeypatch.setattr(server_module, "_run", boom)
    with pytest.raises(LockHeldError):
        server_module.main()


def test_config_error_clean_exit(monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    async def boom() -> None:
        raise ConfigError("bad config")

    monkeypatch.setattr(server_module, "_run", boom)
    with pytest.raises(SystemExit) as exc:
        server_module.main()
    assert exc.value.code == 1
    assert "config error" in capsys.readouterr().err


def test_auth_error_clean_exit(monkeypatch: pytest.MonkeyPatch,
                                capsys: pytest.CaptureFixture[str]) -> None:
    async def boom() -> None:
        raise AuthError("bad token")

    monkeypatch.setattr(server_module, "_run", boom)
    with pytest.raises(SystemExit) as exc:
        server_module.main()
    assert exc.value.code == 1
    assert "auth error" in capsys.readouterr().err


def test_keyboard_interrupt_exit_130(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(server_module, "_run", boom)
    with pytest.raises(SystemExit) as exc:
        server_module.main()
    assert exc.value.code == 130


def test_env_bool() -> None:
    assert server_module._env_bool("___not_set___") is False


def test_build_clients_skips_telegram_when_token_missing_and_polling_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`disable_polling: true` with missing token must NOT crash startup —
    fallback to ntfy-only and log a warning."""
    monkeypatch.delenv("MCGRAM_BOT_TOKEN", raising=False)
    from mcgram.config import BotConfig, NtfyConfig, Settings

    settings = Settings(
        bot=BotConfig(token_env="MCGRAM_BOT_TOKEN",
                      operator_chat_id=1, disable_polling=True),
        ntfy=NtfyConfig(server="https://ntfy.sh", default_topic="mcgram-test"),
    )

    async def _exercise() -> tuple:
        async with server_module._build_clients(settings) as pair:
            return pair

    tg, nt = asyncio.run(_exercise())
    assert tg is None
    assert nt is not None


def test_build_clients_raises_when_token_missing_and_polling_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `disable_polling`, missing token must still be fatal."""
    monkeypatch.delenv("MCGRAM_BOT_TOKEN", raising=False)
    from mcgram.config import BotConfig, NtfyConfig, Settings

    settings = Settings(
        bot=BotConfig(token_env="MCGRAM_BOT_TOKEN", operator_chat_id=1),
        ntfy=NtfyConfig(server="https://ntfy.sh", default_topic="mcgram-test"),
    )

    async def _exercise() -> None:
        async with server_module._build_clients(settings):
            pass

    with pytest.raises(ConfigError):
        asyncio.run(_exercise())
