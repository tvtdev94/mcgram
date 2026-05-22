"""`mcgram doctor` — verify config + transport connectivity end-to-end."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .config import Settings
from .errors import AuthError, ConfigError, NtfyError, TelegramError
from .ntfy_client import NtfyClient
from .tg_client import TelegramClient


def _mask(token: str) -> str:
    return f"***{token[-4:]}" if len(token) > 4 else "***"


def _ok(label: str, detail: str = "") -> str:
    return f"[OK]   {label}" + (f"  {detail}" if detail else "")


def _fail(label: str, detail: str = "") -> str:
    return f"[FAIL] {label}" + (f"  {detail}" if detail else "")


def _skip(label: str, detail: str = "") -> str:
    return f"[SKIP] {label}" + (f"  {detail}" if detail else "")


async def _check_telegram(settings: Settings, rows: list[str]) -> int:
    assert settings.bot is not None
    failures = 0
    try:
        token = settings.resolve_token()
        rows.append(_ok("telegram token", _mask(token)))
    except ConfigError as e:
        if settings.bot.disable_polling:
            rows.append(_skip(
                "telegram",
                f"bot.disable_polling = true and {settings.bot.token_env} unset "
                "— Telegram disabled on this machine (intentional)",
            ))
            return 0
        rows.append(_fail("telegram token", str(e)))
        return 1

    try:
        async with TelegramClient(token, api_root=settings.api_root) as c:
            try:
                me = await c.get_me()
                rows.append(_ok("telegram get_me", f"bot @{me.get('username')}"))
            except (TelegramError, AuthError) as e:
                rows.append(_fail("telegram get_me", str(e)))
                return 1
            except httpx.HTTPError as e:
                rows.append(_fail(
                    "telegram get_me",
                    f"network/SSL error talking to {settings.api_root} "
                    f"(blocked? proxy?): {e}",
                ))
                return 1
            chat_id = settings.bot.operator_chat_id
            rows.append(_ok("telegram operator_chat_id", str(chat_id)))
            try:
                msg = await c.send_message(chat_id, "mcgram doctor: telegram OK")
                rows.append(_ok(
                    "telegram send test", f"message_id={msg.get('message_id')}",
                ))
            except (TelegramError, AuthError) as e:
                rows.append(_fail("telegram send test", str(e)))
                failures += 1
            except httpx.HTTPError as e:
                rows.append(_fail("telegram send test", f"network/SSL error: {e}"))
                failures += 1
    except httpx.HTTPError as e:
        rows.append(_fail(
            "telegram transport",
            f"cannot reach {settings.api_root} (blocked? proxy?): {e}",
        ))
        failures += 1
    return failures


async def _check_ntfy(settings: Settings, rows: list[str]) -> int:
    assert settings.ntfy is not None
    failures = 0
    if not settings.ntfy.default_topic:
        rows.append(_fail("ntfy default_topic", "not set in config"))
        return 1
    token = settings._resolve_ntfy_token()  # noqa: SLF001
    try:
        async with NtfyClient(settings.ntfy.server, access_token=token) as nc:
            try:
                healthy = await nc.health()
            except (NtfyError, AuthError) as e:
                rows.append(_fail("ntfy health", str(e)))
                return 1
            except httpx.HTTPError as e:
                rows.append(_fail("ntfy health", f"network/SSL error: {e}"))
                return 1
            if healthy:
                rows.append(_ok("ntfy health", settings.ntfy.server))
            else:
                rows.append(_fail("ntfy health",
                                  f"non-200 from {settings.ntfy.server}"))
                failures += 1
            try:
                await nc.send_message(
                    settings.ntfy.default_topic,
                    "mcgram doctor: ntfy connection OK",
                    title="mcgram doctor",
                )
                rows.append(_ok(
                    "ntfy send test",
                    f"topic={settings.ntfy.default_topic}  "
                    f"(subscribe in app: "
                    f"{settings.ntfy.server}/{settings.ntfy.default_topic})",
                ))
            except (NtfyError, AuthError) as e:
                rows.append(_fail("ntfy send test", str(e)))
                failures += 1
            except httpx.HTTPError as e:
                rows.append(_fail("ntfy send test", f"network/SSL error: {e}"))
                failures += 1
    except httpx.HTTPError as e:
        rows.append(_fail(
            "ntfy transport",
            f"cannot reach {settings.ntfy.server}: {e}",
        ))
        failures += 1
    return failures


async def _run_checks(settings: Settings) -> tuple[int, list[str]]:
    rows: list[str] = []
    failures = 0
    if settings.bot is not None:
        failures += await _check_telegram(settings, rows)
    else:
        rows.append(_skip("telegram", "no `bot` section in config"))
    if settings.ntfy is not None:
        failures += await _check_ntfy(settings, rows)
    else:
        rows.append(_skip("ntfy", "no `ntfy` section in config"))
    return failures, rows


def _resolve_config_path() -> Path:
    env = os.environ.get("MCGRAM_CONFIG")
    return Path(env).expanduser() if env else Path("~/.mcgram/config.yaml").expanduser()


def doctor() -> int:
    """Run all checks and print a summary. Exit 0 if all OK, 1 otherwise."""
    cfg_path = _resolve_config_path()
    print(f"config:  {cfg_path}")
    if not cfg_path.is_file():
        print(_fail("config file", "not found — run `mcgram init` first"))
        return 1
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
