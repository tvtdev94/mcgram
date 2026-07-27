"""Server bootstrap smoke tests (no real Telegram connection)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcgram import server as server_module
from mcgram.config import Settings
from mcgram.runtime import AppState


def test_server_imports() -> None:
    """Importing server should succeed (Phase 3 modules optional but present)."""
    assert hasattr(server_module, "main")
    assert server_module.SERVER_NAME == "mcgram"


def test_phase2_tool_modules_registered() -> None:
    mods = server_module._phase2_tool_modules()
    names = [m.TOOL_NAME for m in mods]
    assert "send_message" in names
    assert "send_file" in names


def test_wire_phase3_attaches_reminders_but_not_ask_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reminders are safe in every instance; the ask registry is not.

    `ask` only resolves in the process that owns the Telegram poll loop, so
    `PollOwnership` — not `_wire_phase3` — decides when the registry exists.
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
bot:
  operator_chat_id: 1
audit:
  path: {(tmp_path / 'audit.jsonl').as_posix()}
""",
        encoding="utf-8",
    )
    settings = Settings.load(cfg)
    from mcgram.audit import AuditLog
    from mcgram.rate_limiter import RateLimiter
    from mcgram.update_dispatcher import UpdateDispatcher

    fake_client = MagicMock()
    fake_client.answer_callback_query = AsyncMock(return_value=True)
    state = AppState(
        settings=settings,
        client=fake_client,
        dispatcher=UpdateDispatcher(),
        rate=RateLimiter(20),
        audit=AuditLog(settings.audit.path),
    )
    server_module._wire_phase3(state)
    assert state.reminders is not None
    assert state.ask_registry is None
    server_module._shutdown_phase3(state)


def test_resolve_config_path_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCGRAM_CONFIG", raising=False)
    path = server_module._resolve_config_path_str()
    assert path.endswith("config.yaml")


def test_resolve_config_path_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "my.yaml"
    monkeypatch.setenv("MCGRAM_CONFIG", str(custom))
    path = server_module._resolve_config_path_str()
    assert path == str(custom)


def test_load_env_beside_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("bot:\n  operator_chat_id: 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SMOKE_VAR=hello\n", encoding="utf-8")
    monkeypatch.delenv("SMOKE_VAR", raising=False)
    server_module._load_env_beside_config(cfg)
    import os
    assert os.environ.get("SMOKE_VAR") == "hello"


def _state_for(settings: Settings, tmp_path: Path, client: object | None) -> AppState:
    from mcgram.audit import AuditLog
    from mcgram.rate_limiter import RateLimiter
    from mcgram.update_dispatcher import UpdateDispatcher

    return AppState(
        settings=settings,
        client=client,  # type: ignore[arg-type]
        dispatcher=UpdateDispatcher(),
        rate=RateLimiter(20),
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )


def _settings(tmp_path: Path, body: str) -> Settings:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"{body}\naudit:\n  path: {(tmp_path / 'audit.jsonl').as_posix()}\n",
        encoding="utf-8",
    )
    return Settings.load(cfg)


def test_start_polling_skips_lock_when_no_bot(tmp_path: Path) -> None:
    """An ntfy-only instance must never take the poll lock.

    It has nothing to poll, so holding the lock would only starve a real
    Telegram instance of `ask`.
    """
    settings = _settings(tmp_path, "ntfy:\n  default_topic: mcgram-test")
    state = _state_for(settings, tmp_path, None)
    ownership, task = server_module._start_polling(state, settings)
    assert ownership is None and task is None
    assert not settings.lock_path.exists()


def test_start_polling_skips_lock_when_polling_disabled(tmp_path: Path) -> None:
    """Same for `disable_polling` — opted out of polling, so opt out of the lock."""
    settings = _settings(
        tmp_path,
        "bot:\n  operator_chat_id: 1\n  disable_polling: true\nntfy:\n  default_topic: t",
    )
    state = _state_for(settings, tmp_path, MagicMock())
    ownership, task = server_module._start_polling(state, settings)
    assert ownership is None and task is None
    assert not settings.lock_path.exists()


async def test_start_polling_second_instance_boots_degraded(tmp_path: Path) -> None:
    """The reported bug: instance 2 must come up, not exit."""
    from mcgram.poll_ownership import PollOwnership, cancel_task

    settings = _settings(tmp_path, "bot:\n  operator_chat_id: 1")
    holder = PollOwnership(settings.lock_path)
    assert holder.acquire() is True

    client = MagicMock()
    client.answer_callback_query = AsyncMock(return_value=True)
    state = _state_for(settings, tmp_path, client)
    ownership, task = server_module._start_polling(state, settings)
    try:
        assert ownership is not None
        assert state.owns_polling is False       # degraded, but alive
        assert state.ask_registry is None        # `ask` disabled here
        assert state.poll_owner_pid == os.getpid()
    finally:
        await cancel_task(task)
        if ownership is not None:
            ownership.release()
        holder.release()


# Ensure asyncio import is exercised so coverage counts it.
_ = asyncio
