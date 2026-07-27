"""CLI dispatcher for mcgram. Lazy-imports heavy submodules for <500ms startup."""

from __future__ import annotations

import sys

from . import __version__

_HELP = """\
mcgram — notification bridge MCP for Claude Code (Telegram + ntfy.sh).

USAGE:
  mcgram                          start the MCP stdio server (expects MCGRAM_CONFIG)
  mcgram init [--force]           scaffold ~/.mcgram/ + install Claude Code skill
  mcgram doctor                   check config + transport connectivity (sends test pings)
  mcgram audit [opts]             analyze audit.jsonl (--since, --tool, --rejected, --tail)
  mcgram channel list             list configured channels (both transports)
  mcgram channel add NAME CHAT_ID add a Telegram channel
  mcgram channel add-ntfy NAME    add a ntfy.sh channel (auto-generates topic)
  mcgram channel remove NAME      remove a channel
  mcgram install-skill [--force]  install / reinstall ~/.claude/skills/mcgram/SKILL.md
  mcgram clear-lock               remove stale ~/.mcgram/.lock (frees Telegram polling)
  mcgram --version                print version
  mcgram --help                   print this help
"""


def main() -> None:
    args = sys.argv[1:]
    if args:
        first = args[0]
        if first in {"-V", "--version", "version"}:
            print(f"mcgram {__version__}")
            return
        if first in {"-h", "--help", "help"}:
            sys.stdout.write(_HELP)
            return
        if first == "init":
            from .cli_init import init_config
            force = "--force" in args[1:]
            sys.exit(init_config(force=force))
        if first == "doctor":
            from .cli_doctor import doctor
            sys.exit(doctor())
        if first == "audit":
            from .cli_audit import main as audit_main
            sys.exit(audit_main(args[1:]))
        if first == "channel":
            from .cli_channel import main as channel_main
            sys.exit(channel_main(args[1:]))
        if first == "install-skill":
            from .skill_installer import install_skill
            force = "--force" in args[1:]
            sys.exit(install_skill(force=force))
        if first == "clear-lock":
            # The lock now only gates Telegram polling, not startup — a stale
            # lock costs `ask`, never the whole server. Running instances
            # re-check periodically, so they pick polling up on their own.
            from pathlib import Path
            lock = Path("~/.mcgram/.lock").expanduser()
            if lock.exists():
                lock.unlink()
                print(f"removed  {lock}")
                print("a running mcgram will take over Telegram polling within ~30s")
            else:
                print(f"no lock file at {lock} (already clean)")
            sys.exit(0)
        print(f"unknown argument: {first}. Try `mcgram --help`.", file=sys.stderr)
        sys.exit(2)
    # No args → run MCP stdio server.
    from .server import main as server_main
    server_main()


if __name__ == "__main__":
    main()
