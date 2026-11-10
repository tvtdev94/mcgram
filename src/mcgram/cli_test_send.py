"""`mcgram test-send` — send one test message through the normal send path.

Unlike `mcgram doctor`, which probes each transport with its own client calls,
this goes through the `send_message` tool exactly as Claude Code would: channel
resolution, caps, rate limit, dispatch, error mapping and the audit log.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from dotenv import load_dotenv

from .audit import AuditLog
from .cli_doctor import _fail, _ok, _resolve_config_path
from .config import Settings
from .errors import AuthError, ConfigError, MCGramError
from .rate_limiter import RateLimiter
from .runtime import AppState
from .update_dispatcher import UpdateDispatcher


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mcgram test-send",
        description="Send a test message to a channel through the normal send path.",
    )
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("channel", nargs="?", help="channel name (see `mcgram channel list`)")
    target.add_argument("--all", action="store_true", help="send to every configured channel")
    p.add_argument("--text", help="message text (default: a short test line)")
    return p.parse_args(argv)


def _describe(name: str, out: dict[str, Any]) -> tuple[bool, str]:
    if out.get("ok"):
        return True, _ok(name, f"{out.get('transport')}  message_id={out.get('message_id')}")
    detail = str(out.get("error"))
    if out.get("reason"):
        detail += f": {out['reason']}"
    return False, _fail(name, detail)


async def _send_all(
    settings: Settings, names: list[str], text: str | None
) -> tuple[int, list[str]]:
    # Imported here: the server module pulls in the MCP SDK, which the other
    # CLI commands do not need.
    from .server import _build_clients
    from .tools import send_message

    audit = AuditLog(
        settings.audit.path,
        rotate_mb=settings.audit.rotate_mb,
        timezone=settings.audit.timezone,
        redact_text=settings.audit.redact_text,
        retention_days=settings.audit.retention_days,
    )
    rows: list[str] = []
    failures = 0
    async with _build_clients(settings) as (tg_client, ntfy_client, discord_client):
        state = AppState(
            settings=settings,
            dispatcher=UpdateDispatcher(),
            rate=RateLimiter(max(settings.defaults.rate_limit_per_min, len(names))),
            audit=audit,
            client=tg_client,
            ntfy_client=ntfy_client,
            discord_client=discord_client,
        )
        for name in names:
            try:
                out = await send_message.handle(
                    state, text=text or f"mcgram test-send: {name} OK", channel=name,
                )
            except MCGramError as e:
                out = {"error": type(e).__name__, "reason": str(e)}
            ok, row = _describe(name, out)
            failures += 0 if ok else 1
            rows.append(row)
    return failures, rows


def main(argv: list[str]) -> int:
    """Exit 0 when every send succeeded, 1 on any failure, 2 on usage errors."""
    args = _parse(argv)
    cfg_path = _resolve_config_path()
    if not cfg_path.is_file():
        print(_fail("config file", f"{cfg_path} not found — run `mcgram init` first"))
        return 1
    env = cfg_path.parent / ".env"
    if env.is_file():
        load_dotenv(env, override=False)
    try:
        settings = Settings.load(cfg_path)
    except ConfigError as e:
        print(_fail("config loaded", str(e)))
        return 1
    names = sorted(settings.channels) if args.all else [args.channel]
    try:
        failures, rows = asyncio.run(_send_all(settings, names, args.text))
    except (ConfigError, AuthError) as e:
        print(_fail("transport setup", str(e)))
        return 1
    for row in rows:
        print(row)
    print("RESULT:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1
