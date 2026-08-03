"""`mcgram discord` — Discord-specific settings (the @mention name -> id registry).

Mentions live in `discord.mentions` in config.yaml. User IDs are not secrets (a
webhook URL is), so unlike webhooks they belong in the YAML, not ~/.mcgram/.env.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

from .cli_channel import _config_path, _load_raw, _save_raw

# Discord snowflake user id: 15-25 digits (17-19 in practice; kept lenient).
_USER_ID_RE = re.compile(r"^\d{15,25}$")


def _mentions(data: dict[str, Any]) -> dict[str, str]:
    return dict((data.get("discord") or {}).get("mentions") or {})


def cmd_mention_add(args: argparse.Namespace) -> int:
    name = args.name.strip()
    if not name:
        print("mention name is required.", file=sys.stderr)
        return 2
    if not _USER_ID_RE.match(args.user_id):
        print(
            "not a Discord user id (expected 15-25 digits). Enable Developer Mode "
            "(Settings → Advanced), right-click a user → Copy ID.",
            file=sys.stderr,
        )
        return 2
    path = _config_path()
    data = _load_raw(path)
    discord = dict(data.get("discord") or {})
    mentions = _mentions(data)
    mentions[name] = args.user_id
    discord["mentions"] = mentions
    data["discord"] = discord
    _save_raw(path, data)
    print(f"added  {name} -> {args.user_id}")
    print("next   restart Claude Code so the MCP server reloads the mention registry")
    return 0


def cmd_mention_list(args: argparse.Namespace) -> int:
    _ = args
    mentions = _mentions(_load_raw(_config_path()))
    if not mentions:
        print("(no mentions registered)")
        return 0
    width = max(len(n) for n in mentions)
    for name in sorted(mentions):
        print(f"  {name:<{width}}  {mentions[name]}")
    return 0


def cmd_mention_remove(args: argparse.Namespace) -> int:
    path = _config_path()
    data = _load_raw(path)
    mentions = _mentions(data)
    if args.name not in mentions:
        print(f"mention '{args.name}' not found.", file=sys.stderr)
        return 1
    mentions.pop(args.name)
    discord = dict(data.get("discord") or {})
    discord["mentions"] = mentions
    data["discord"] = discord
    _save_raw(path, data)
    print(f"removed  {args.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mcgram discord", description="Manage Discord settings")
    sub = p.add_subparsers(dest="group", required=True)

    mention = sub.add_parser("mention", help="manage the @mention name -> user id registry")
    msub = mention.add_subparsers(dest="action", required=True)

    madd = msub.add_parser("add", help="register a @mention target")
    madd.add_argument("name", help="short name to reference (e.g. 'alice')")
    madd.add_argument("user_id", help="Discord user id (15-25 digits)")

    msub.add_parser("list", help="list registered mentions")

    mrm = msub.add_parser("remove", help="remove a registered mention")
    mrm.add_argument("name", help="mention name")

    args = p.parse_args(argv)
    handlers = {
        ("mention", "add"): cmd_mention_add,
        ("mention", "list"): cmd_mention_list,
        ("mention", "remove"): cmd_mention_remove,
    }
    return handlers[(args.group, args.action)](args)
