"""`mcgram channel` — manage named Telegram destinations in ~/.mcgram/config.yaml."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from .config import DEFAULT_CHANNEL


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
    """Return channels including the implicit `default` from bot.operator_chat_id."""
    channels = dict(data.get("channels") or {})
    if DEFAULT_CHANNEL not in channels:
        op = (data.get("bot") or {}).get("operator_chat_id")
        if op is not None:
            channels[DEFAULT_CHANNEL] = {
                "chat_id": op,
                "description": "Auto-created from bot.operator_chat_id",
            }
    return channels


def cmd_list(args: argparse.Namespace) -> int:
    data = _load_raw(_config_path())
    channels = _all_channels(data)
    if not channels:
        print("(no channels configured)")
        return 0
    name_w = max(len(n) for n in channels)
    for name in sorted(channels):
        c = channels[name]
        desc = c.get("description") or ""
        print(f"  {name:<{name_w}}  chat_id={c['chat_id']:<15}  {desc}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
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
    channels[args.name] = {"chat_id": args.chat_id}
    if args.description:
        channels[args.name]["description"] = args.description
    data["channels"] = channels
    _save_raw(path, data)
    print(f"added  {args.name}  chat_id={args.chat_id}")
    return 0


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

    add = sub.add_parser("add", help="add or update a channel")
    add.add_argument("name", help="channel name (e.g. 'team', 'bugs')")
    add.add_argument("chat_id", type=int, help="Telegram chat_id (int)")
    add.add_argument("--description", "-d", help="optional human description")
    add.add_argument("--force", action="store_true", help="overwrite existing")

    rm = sub.add_parser("remove", help="remove a channel")
    rm.add_argument("name", help="channel name")

    args = p.parse_args(argv)
    handlers = {"list": cmd_list, "add": cmd_add, "remove": cmd_remove}
    return handlers[args.action](args)
