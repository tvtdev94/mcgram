"""`mcgram doctor` — verify config + bot connectivity end-to-end."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .config import Settings
from .errors import AuthError, ConfigError, TelegramError
from .tg_client import TelegramClient


def _mask(token: str) -> str:
    return f"***{token[-4:]}" if len(token) > 4 else "***"


def _ok(label: str, detail: str = "") -> str:
    return f"[OK]   {label}" + (f"  {detail}" if detail else "")


def _fail(label: str, detail: str = "") -> str:
    return f"[FAIL] {label}" + (f"  {detail}" if detail else "")


async def _run_checks(settings: Settings) -> tuple[int, list[str]]:
    rows: list[str] = []
    failures = 0
    try:
        token = settings.resolve_token()
        rows.append(_ok("token resolved", _mask(token)))
    except ConfigError as e:
        rows.append(_fail("token resolved", str(e)))
        return 1, rows

    async with TelegramClient(token, api_root=settings.api_root) as c:
        try:
            me = await c.get_me()
            rows.append(_ok("get_me", f"bot @{me.get('username')}"))
        except (TelegramError, AuthError) as e:
            rows.append(_fail("get_me", str(e)))
            return 1, rows

        chat_id = settings.bot.operator_chat_id
        rows.append(_ok("operator_chat_id", str(chat_id)))

        try:
            msg = await c.send_message(chat_id, "🩺 mcgram doctor: connection OK")
            rows.append(_ok("send test message", f"message_id={msg.get('message_id')}"))
        except (TelegramError, AuthError) as e:
            rows.append(_fail("send test message", str(e)))
            failures += 1

    return failures, rows


def _resolve_config_path() -> Path:
    env = os.environ.get("MCGRAM_CONFIG")
    return Path(env).expanduser() if env else Path("~/.mcgram/config.yaml").expanduser()


def doctor() -> int:
    """Run all checks and print a summary table. Exit 0 if all OK, 1 otherwise."""
    cfg_path = _resolve_config_path()
    print(f"config:  {cfg_path}")
    if not cfg_path.is_file():
        print(_fail("config file", "not found — run `mcgram init` first"))
        return 1
    # Load .env sitting next to the config
    env = cfg_path.parent / ".env"
    if env.is_file():
        load_dotenv(env, override=False)
    try:
        settings = Settings.load(cfg_path)
        print(_ok("config loaded"))
    except ConfigError as e:
        print(_fail("config loaded", str(e)))
        return 1

    failures, rows = asyncio.run(_run_checks(settings))
    print()
    for row in rows:
        print(row)
    print()
    print("RESULT:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1


# Type-only re-export to suppress unused-import linter on bare imports
_ = Any
