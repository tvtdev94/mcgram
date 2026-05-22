"""CLI dispatcher for mcgram. Lazy-imports heavy submodules for <500ms startup."""

from __future__ import annotations

import sys

from . import __version__

_HELP = """\
mcgram — Telegram bridge MCP for Claude Code.

USAGE:
  mcgram                          start the MCP stdio server (expects MCGRAM_CONFIG)
  mcgram init [--force]           scaffold ~/.mcgram/ + install Claude Code skill
  mcgram doctor                   check config + bot connectivity (sends a test message)
  mcgram audit [opts]             analyze audit.jsonl (--since, --tool, --rejected, --tail)
  mcgram channel <action> [...]   manage named channels (list | add NAME CHAT_ID | remove NAME)
  mcgram install-skill [--force]  install / reinstall ~/.claude/skills/mcgram/SKILL.md
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
        print(f"unknown argument: {first}. Try `mcgram --help`.", file=sys.stderr)
        sys.exit(2)
    # No args → run MCP stdio server.
    from .server import main as server_main
    server_main()


if __name__ == "__main__":
    main()
