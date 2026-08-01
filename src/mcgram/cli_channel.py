"""`mcgram channel` — manage named destinations (telegram + ntfy + discord)."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any

import yaml

from .config import DEFAULT_CHANNEL
from .discord_client import DiscordClient
from .env_file import upsert_env_var

# A Discord webhook URL: .../api/webhooks/<id>/<token>. Accept the legacy
# discordapp.com host and optional regional / api-version prefixes.
_WEBHOOK_RE = re.compile(
    r"^https://(?:\w+\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+$"
)
_WEBHOOK_EXAMPLE = "https://discord.com/api/webhooks/123456789/AbCdEf-token"


def _config_path() -> Path:
    env = os.environ.get("MCGRAM_CONFIG")
    return Path(env).expanduser() if env else Path("~/.mcgram/config.yaml").expanduser()


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.is_file():
        print(f"config not found: {path} — run `mcgram init` first.", file=sys.stderr)
        sys.exit(2)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_raw(path: Path, data: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _all_channels(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return channels including the implicit `default` derived from bot/ntfy config."""
    channels = {k: dict(v) for k, v in (data.get("channels") or {}).items()}
    if DEFAULT_CHANNEL not in channels:
        bot = data.get("bot") or {}
        ntfy = data.get("ntfy") or {}
        if bot.get("operator_chat_id") is not None:
            channels[DEFAULT_CHANNEL] = {
                "transport": "telegram",
                "chat_id": bot["operator_chat_id"],
                "description": "Auto-created from bot.operator_chat_id",
            }
        elif ntfy.get("default_topic"):
            channels[DEFAULT_CHANNEL] = {
                "transport": "ntfy",
                "ntfy_topic": ntfy["default_topic"],
                "description": "Auto-created from ntfy.default_topic",
            }
        else:
            discord_chans = [
                (k, v) for k, v in channels.items()
                if v.get("transport") == "discord"
            ]
            if len(discord_chans) == 1:
                _k, v = discord_chans[0]
                channels[DEFAULT_CHANNEL] = {
                    "transport": "discord",
                    "discord_webhook_env": v.get("discord_webhook_env"),
                    "description": "Auto-created from the sole Discord channel",
                }
    # default transport when omitted = telegram (preserves legacy semantics)
    for ch in channels.values():
        ch.setdefault("transport", "telegram")
    return channels


def _format_endpoint(ch: dict[str, Any]) -> str:
    transport = ch.get("transport")
    if transport == "ntfy":
        return f"topic={ch.get('ntfy_topic', '?')}"
    if transport == "discord":
        # Never resolve/print the URL — only the env var name it lives in.
        return f"env={ch.get('discord_webhook_env', '?')}"
    return f"chat_id={ch.get('chat_id', '?')}"


def cmd_list(args: argparse.Namespace) -> int:
    _ = args
    data = _load_raw(_config_path())
    channels = _all_channels(data)
    if not channels:
        print("(no channels configured)")
        return 0
    name_w = max(len(n) for n in channels)
    for name in sorted(channels):
        c = channels[name]
        desc = c.get("description") or ""
        endpoint = _format_endpoint(c)
        transport = c.get("transport", "telegram")
        print(f"  {name:<{name_w}}  {transport:<8}  {endpoint:<32}  {desc}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    """Add a Telegram channel (legacy form: positional chat_id)."""
    if args.name == DEFAULT_CHANNEL:
        print(f"'{DEFAULT_CHANNEL}' is reserved — change bot.operator_chat_id instead.",
              file=sys.stderr)
        return 2
    path = _config_path()
    data = _load_raw(path)
    channels = dict(data.get("channels") or {})
    if args.name in channels and not args.force:
        print(f"channel '{args.name}' already exists (use --force to overwrite).",
              file=sys.stderr)
        return 1
    entry: dict[str, Any] = {"chat_id": args.chat_id}
    if args.description:
        entry["description"] = args.description
    channels[args.name] = entry
    data["channels"] = channels
    _save_raw(path, data)
    print(f"added  {args.name}  chat_id={args.chat_id}")
    return 0


def cmd_add_ntfy(args: argparse.Namespace) -> int:
    """Add a ntfy.sh channel. Auto-generates topic if not provided."""
    if args.name == DEFAULT_CHANNEL:
        print(f"'{DEFAULT_CHANNEL}' is reserved — change ntfy.default_topic instead.",
              file=sys.stderr)
        return 2
    path = _config_path()
    data = _load_raw(path)
    channels = dict(data.get("channels") or {})
    if args.name in channels and not args.force:
        print(f"channel '{args.name}' already exists (use --force to overwrite).",
              file=sys.stderr)
        return 1
    topic = args.topic or f"mcgram-{secrets.token_hex(8)}"
    entry: dict[str, Any] = {"transport": "ntfy", "ntfy_topic": topic}
    if args.description:
        entry["description"] = args.description
    channels[args.name] = entry
    data["channels"] = channels
    _save_raw(path, data)
    server = (data.get("ntfy") or {}).get("server") or "https://ntfy.sh"
    print(f"added  {args.name}  topic={topic}")
    print(f"       subscribe in ntfy app: {server}/{topic}")
    return 0


def _env_name_for(name: str) -> str:
    """Derive an env var name from a channel name: `eve` -> MCGRAM_DISCORD_WEBHOOK_EVE."""
    slug = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
    return f"MCGRAM_DISCORD_WEBHOOK_{slug}"


def _probe_webhook(url: str) -> dict[str, Any] | None:
    """GET the webhook to confirm it is live. Returns metadata or None."""
    async def _run() -> dict[str, Any] | None:
        async with DiscordClient() as dc:
            return await dc.health(url)

    try:
        return asyncio.run(_run())
    except Exception:
        return None


def _add_discord(
    *,
    name: str,
    webhook: str,
    env_name: str | None,
    description: str | None,
    force: bool,
    config_path: Path,
) -> int:
    """Validate a webhook, then write the credential to .env and the channel to
    config.yaml. The URL is verified live before anything is persisted, and it is
    never echoed to stdout."""
    if name == DEFAULT_CHANNEL:
        print(f"'{DEFAULT_CHANNEL}' is reserved — pick another channel name.",
              file=sys.stderr)
        return 2
    if not webhook:
        print("webhook URL is required.", file=sys.stderr)
        return 2
    if not _WEBHOOK_RE.match(webhook):
        print(f"not a Discord webhook URL. Expected shape:\n  {_WEBHOOK_EXAMPLE}",
              file=sys.stderr)
        return 2

    data = _load_raw(config_path)
    channels = dict(data.get("channels") or {})
    if name in channels and not force:
        print(f"channel '{name}' already exists (use --force to overwrite).",
              file=sys.stderr)
        return 1

    meta = _probe_webhook(webhook)
    if meta is None:
        print("webhook check failed — Discord did not accept this URL. "
              "Nothing was written.", file=sys.stderr)
        return 1

    resolved_env = env_name or _env_name_for(name)
    env_path = config_path.parent / ".env"
    upsert_env_var(env_path, resolved_env, webhook)

    entry: dict[str, Any] = {"transport": "discord", "discord_webhook_env": resolved_env}
    if description:
        entry["description"] = description
    channels[name] = entry
    data["channels"] = channels
    data.setdefault("discord", {"username": "mcgram"})
    _save_raw(config_path, data)

    print(f"webhook  {name} -> channel {meta.get('channel_id')} ({meta.get('name')})")
    print(f"env      {resolved_env}  (stored in {env_path})")
    print("next     restart Claude Code so the MCP server reloads the new channel")
    return 0


def cmd_add_discord(args: argparse.Namespace) -> int:
    """Add a Discord webhook channel. Prompts for the URL when --webhook is absent."""
    webhook = args.webhook
    if webhook:
        print("warning  --webhook puts the URL in your shell history; the "
              "interactive prompt is safer.", file=sys.stderr)
    else:
        try:
            webhook = getpass.getpass("Discord webhook URL: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\naborted — use --webhook when running non-interactively.",
                  file=sys.stderr)
            return 2
    return _add_discord(
        name=args.name, webhook=webhook, env_name=args.env_name,
        description=args.description, force=args.force, config_path=_config_path(),
    )


def interactive_add_discord(config_path: Path) -> None:
    """TTY-only Discord setup loop for `mcgram init`. No-op when stdin is not a
    terminal (tests, CI, pipes) so non-interactive init is never blocked."""
    if not sys.stdin.isatty():
        return
    while True:
        try:
            ans = input("Add a Discord channel? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if ans not in {"y", "yes"}:
            return
        name = input("  channel name (e.g. eve): ").strip()
        if not name:
            print("  name is required; skipping.")
            continue
        try:
            webhook = getpass.getpass("  Discord webhook URL: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        _add_discord(
            name=name, webhook=webhook, env_name=None,
            description=None, force=False, config_path=config_path,
        )


def cmd_remove(args: argparse.Namespace) -> int:
    if args.name == DEFAULT_CHANNEL:
        print(f"cannot remove reserved channel '{DEFAULT_CHANNEL}'.", file=sys.stderr)
        return 2
    path = _config_path()
    data = _load_raw(path)
    channels = dict(data.get("channels") or {})
    if args.name not in channels:
        print(f"channel '{args.name}' not found.", file=sys.stderr)
        return 1
    channels.pop(args.name)
    data["channels"] = channels
    _save_raw(path, data)
    print(f"removed  {args.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mcgram channel", description="Manage named channels")
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("list", help="list configured channels")

    add = sub.add_parser("add", help="add or update a Telegram channel")
    add.add_argument("name", help="channel name (e.g. 'team', 'bugs')")
    add.add_argument("chat_id", type=int, help="Telegram chat_id (int)")
    add.add_argument("--description", "-d", help="optional human description")
    add.add_argument("--force", action="store_true", help="overwrite existing")

    addn = sub.add_parser("add-ntfy", help="add or update a ntfy.sh channel")
    addn.add_argument("name", help="channel name (e.g. 'alerts')")
    addn.add_argument("--topic", help="explicit ntfy topic; random if omitted")
    addn.add_argument("--description", "-d", help="optional human description")
    addn.add_argument("--force", action="store_true", help="overwrite existing")

    addd = sub.add_parser("add-discord", help="add or update a Discord webhook channel")
    addd.add_argument("name", help="channel name (e.g. 'eve')")
    addd.add_argument("--webhook", help="webhook URL; prompted securely if omitted")
    addd.add_argument("--env-name", help="override the derived env var name")
    addd.add_argument("--description", "-d", help="optional human description")
    addd.add_argument("--force", action="store_true", help="overwrite existing")

    rm = sub.add_parser("remove", help="remove a channel")
    rm.add_argument("name", help="channel name")

    args = p.parse_args(argv)
    handlers = {
        "list": cmd_list,
        "add": cmd_add,
        "add-ntfy": cmd_add_ntfy,
        "add-discord": cmd_add_discord,
        "remove": cmd_remove,
    }
    return handlers[args.action](args)
