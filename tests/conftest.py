"""Shared fixtures for mcgram tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mcgram.audit import AuditLog
from mcgram.config import Settings
from mcgram.rate_limiter import RateLimiter
from mcgram.runtime import AppState
from mcgram.tg_client import TelegramClient
from mcgram.update_dispatcher import UpdateDispatcher


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    cfg = tmp_path / "config.yaml"
    audit_path = (tmp_path / "audit.jsonl").as_posix()
    cfg.write_text(
        f"""
bot:
  token_env: TEST_TOKEN
  operator_chat_id: 12345
defaults:
  rate_limit_per_min: 20
audit:
  path: {audit_path}
api_root: https://api.telegram.org
""",
        encoding="utf-8",
    )
    return Settings.load(cfg)


@pytest.fixture
def audit_log(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
async def tg_client(settings: Settings) -> Any:
    async with TelegramClient("fake-token", api_root=settings.api_root) as c:
        yield c


@pytest.fixture
async def app_state(settings: Settings, audit_log: AuditLog, tg_client: TelegramClient) -> AppState:
    return AppState(
        settings=settings,
        client=tg_client,
        dispatcher=UpdateDispatcher(),
        rate=RateLimiter(settings.defaults.rate_limit_per_min),
        audit=audit_log,
    )
