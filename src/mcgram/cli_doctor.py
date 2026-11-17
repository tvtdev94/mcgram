"""`mcgram doctor` — verify config + transport connectivity end-to-end."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .broker_wiring import ASK_NAMESPACE, REMINDER_NAMESPACE, safe_spool
from .config import DEFAULT_CHANNEL, Settings
from .discord_client import DiscordClient
from .errors import (
    AuthError,
    ConfigError,
    DiscordError,
    NtfyError,
    TelegramError,
    TransportError,
)
from .ntfy_client import NtfyClient
from .tg_client import TelegramClient


def _mask(token: str) -> str:
    return f"***{token[-4:]}" if len(token) > 4 else "***"


def _ok(label: str, detail: str = "") -> str:
    return f"[OK]   {label}" + (f"  {detail}" if detail else "")


def _fail(label: str, detail: str = "") -> str:
    return f"[FAIL] {label}" + (f"  {detail}" if detail else "")


_NO_SEND = "not sent — run `mcgram doctor --send` to deliver a test message"


def _skip(label: str, detail: str = "") -> str:
    return f"[SKIP] {label}" + (f"  {detail}" if detail else "")


async def _check_telegram(settings: Settings, rows: list[str], send: bool) -> int:
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
            if not send:
                rows.append(_skip("telegram send test", _NO_SEND))
                return failures
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


async def _check_ntfy(settings: Settings, rows: list[str], send: bool) -> int:
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
            if settings.ntfy.ask.enabled:
                failures += await _check_ntfy_ask(settings, nc, rows, send)
            if not send:
                rows.append(_skip("ntfy send test", _NO_SEND))
                return failures
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


async def _check_ntfy_ask(
    settings: Settings, nc: NtfyClient, rows: list[str], send: bool
) -> int:
    """What `ask` over ntfy enforces, checked up front: token, https, the two
    topics' anonymous access, and that the token can read the reply topic. All
    of these are reads, so they run without `send`."""
    assert settings.ntfy is not None
    ntfy = settings.ntfy
    reply_topic = ntfy.ask.reply_topic
    assert reply_topic is not None  # Settings validation guarantees it
    failures = 0
    if nc.has_token:
        rows.append(_ok("ntfy ask token", f"env {ntfy.access_token_env}"))
    else:
        rows.append(_fail("ntfy ask token", f"env {ntfy.access_token_env} is not set"))
        failures += 1
    if not ntfy.server.startswith("https://"):
        rows.append(_fail("ntfy ask https",
                          f"{ntfy.server} is not https:// — ask over ntfy refuses it"))
        failures += 1
    # `_check_ntfy` has already required default_topic, the fallback topic.
    question_topics = sorted({
        ch.ntfy_topic or ntfy.default_topic or "" for ch in settings.channels.values()
        if ch.transport == "ntfy"
    } - {""})
    checks = [("ntfy ask question topic protected", t,
               "reserve the topic / deny anonymous read") for t in question_topics]
    checks.append(("ntfy ask reply topic protected", reply_topic,
                   "reserve it with everyone write-only, read only by your user"))
    for label, topic, fix in checks:
        try:
            denied = await nc.anonymous_read_denied(topic)
        except (NtfyError, TransportError) as e:
            rows.append(_fail(label, f"topic={topic}: {e}"))
            failures += 1
            continue
        if denied:
            rows.append(_ok(label, f"topic={topic}"))
        else:
            rows.append(_fail(label, f"topic={topic} is readable anonymously — {fix}"))
            failures += 1
    if nc.has_token:
        # mcgram listens for taps as the token's user; without read access the
        # first ask fails with ntfy_auth.
        label = "ntfy ask token reads reply topic"
        try:
            await nc.poll(reply_topic, since="none")
            rows.append(_ok(label, f"topic={reply_topic}"))
        except AuthError:
            rows.append(_fail(label, f"topic={reply_topic}: the token's user may not read "
                                     "it — give that user read access to the reply topic"))
            failures += 1
        except (NtfyError, TransportError) as e:
            rows.append(_fail(label, f"topic={reply_topic}: {e}"))
            failures += 1
    label = "ntfy ask reply topic accepts anonymous publish"
    if not send:
        rows.append(_skip(label, _NO_SEND))
        return failures
    try:
        # As a phone tap does: no credential. Running instances ignore it (no `q`).
        async with NtfyClient(ntfy.server) as anonymous:
            await anonymous.send_message(reply_topic, '{"probe":"mcgram doctor"}')
        rows.append(_ok(label, f"topic={reply_topic}"))
    except AuthError:
        rows.append(_fail(label, "phone taps will be refused; set everyone write-only "
                                 "on the reply topic"))
        failures += 1
    except (NtfyError, TransportError) as e:
        rows.append(_fail(label, str(e)))
        failures += 1
    return failures


async def _check_discord(settings: Settings, rows: list[str], send: bool) -> int:
    """Verify every Discord channel: env var set, webhook live, and with `send` a
    test message delivered.

    Prints channel name + channel_id only — never the webhook URL or token."""
    discord = {
        name: ch for name, ch in settings.channels.items()
        if ch.transport == "discord"
    }
    # Drop an auto-seeded `default` that merely mirrors a named channel's webhook,
    # so the same endpoint isn't probed and messaged twice.
    if DEFAULT_CHANNEL in discord:
        dfl_env = discord[DEFAULT_CHANNEL].discord_webhook_env
        if any(n != DEFAULT_CHANNEL and c.discord_webhook_env == dfl_env
               for n, c in discord.items()):
            discord.pop(DEFAULT_CHANNEL)
    discord_channels = sorted(discord.items())
    failures = 0
    async with DiscordClient() as dc:
        for name, ch in discord_channels:
            try:
                dest = settings.resolve_destination(name)
            except ConfigError as e:
                rows.append(_fail(f"discord {name}", str(e)))
                failures += 1
                continue
            url = dest.discord_webhook_url
            assert url is not None
            # health() swallows network errors and returns None, so a None here
            # can mean either an invalid webhook or an unreachable host — keep the
            # message neutral rather than blaming the env var.
            meta = await dc.health(url)
            if meta is None:
                rows.append(_fail(
                    f"discord {name} health",
                    f"could not verify webhook — unreachable or invalid "
                    f"(check env {ch.discord_webhook_env} and network)",
                ))
                failures += 1
                continue
            rows.append(_ok(
                f"discord {name}",
                f"channel {meta.get('channel_id')} ({meta.get('name')})",
            ))
            if not send:
                rows.append(_skip(f"discord {name} send test", _NO_SEND))
                continue
            try:
                sent = await dc.send_message(
                    url, f"mcgram doctor: discord OK — {name}",
                    username=dest.discord_username, avatar_url=dest.discord_avatar_url,
                )
                rows.append(_ok(
                    f"discord {name} send test", f"message_id={sent.get('id')}",
                ))
            except DiscordError as e:
                rows.append(_fail(f"discord {name} send test", e.description))
                failures += 1
            except httpx.HTTPError as e:
                # Interpolate the type name only — a raw httpx error can echo the
                # request URL (which carries the webhook token).
                rows.append(_fail(
                    f"discord {name} send test", f"network/SSL error ({type(e).__name__})",
                ))
                failures += 1
    return failures


async def _run_checks(settings: Settings, send: bool) -> tuple[int, list[str]]:
    rows: list[str] = []
    failures = 0
    if settings.bot is not None:
        failures += await _check_telegram(settings, rows, send)
    else:
        rows.append(_skip("telegram", "no `bot` section in config"))
    if settings.ntfy is not None:
        failures += await _check_ntfy(settings, rows, send)
    else:
        rows.append(_skip("ntfy", "no `ntfy` section in config"))
    if any(ch.transport == "discord" for ch in settings.channels.values()):
        failures += await _check_discord(settings, rows, send)
    failures += _check_spool(settings, rows)
    return failures, rows


def _check_spool(settings: Settings, rows: list[str]) -> int:
    """Reminders always persist to the spool; the ask broker uses it when enabled."""
    namespaces = [REMINDER_NAMESPACE]
    if settings.broker.enabled:
        namespaces.append(ASK_NAMESPACE)
    failures = 0
    for ns in namespaces:
        spool, reason = safe_spool(settings, ns)
        if spool is None:
            rows.append(_fail(f"spool/{ns}", reason))
            failures += 1
        else:
            rows.append(_ok(f"spool/{ns}", str(spool.dir)))
    if not settings.broker.enabled:
        rows.append(_skip("broker", "broker.enabled is false (default)"))
    return failures


def _resolve_config_path() -> Path:
    env = os.environ.get("MCGRAM_CONFIG")
    return Path(env).expanduser() if env else Path("~/.mcgram/config.yaml").expanduser()


def doctor(*, send: bool = False) -> int:
    """Run all checks and print a summary. Exit 0 if all OK, 1 otherwise.

    Checks are read-only by default; `send` also delivers one test message to
    every configured transport and channel.
    """
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

    failures, rows = asyncio.run(_run_checks(settings, send))
    print()
    for row in rows:
        print(row)
    print()
    print("RESULT:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1
