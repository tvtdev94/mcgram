"""Server bootstrap smoke tests (no real Telegram connection)."""

from __future__ import annotations

import asyncio
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


def test_wire_phase3_attaches_registries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    assert state.ask_registry is not None
    assert state.reminders is not None
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


# Ensure asyncio import is exercised so coverage counts it.
_ = asyncio
