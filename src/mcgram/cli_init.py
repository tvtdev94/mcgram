"""`mcgram init` — scaffold ~/.mcgram/, install skill, register MCP with Claude Code."""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from importlib import resources
from pathlib import Path

from .skill_installer import install_skill


def _bundled_config_yaml() -> str:
    data = resources.files("mcgram.data").joinpath("config.example.yaml")
    return data.read_text(encoding="utf-8")


_ENV_TEMPLATE = (
    "# mcgram credentials — never commit.\n"
    "# Get token from @BotFather on Telegram (/newbot).\n"
    "MCGRAM_BOT_TOKEN=\n"
)


_NEXT_STEPS = """\

Next steps:
  1. Get a bot token from @BotFather on Telegram (/newbot)
     (VI: gõ /newbot, đặt tên, copy token)
  2. Get your chat ID:
       - Open Telegram, /start your new bot
       - Visit: https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
       - Copy `chat.id` from the JSON
  3. Edit {env}  → paste MCGRAM_BOT_TOKEN
  4. Edit {cfg} → set operator_chat_id
  5. Test:  mcgram doctor
  6. Restart Claude Code → /mcp → mcgram appears
"""


def _claude_cli_available() -> bool:
    return shutil.which("claude") is not None


def _is_already_registered() -> bool:
    """Return True if `mcgram` already in `claude mcp list`."""
    try:
        result = subprocess.run(
            ["claude", "mcp", "list"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False
    return any(
        line.strip().startswith("mcgram:") or line.strip().startswith("mcgram ")
        for line in (result.stdout or "").splitlines()
    )


def _register_with_claude_code(cfg: Path, *, force: bool) -> None:
    """Run `claude mcp add` so the user doesn't have to. No-ops if claude CLI absent."""
    if not _claude_cli_available():
        print("skip     `claude` CLI not found — run `claude mcp add` manually later")
        return

    already = _is_already_registered()
    if already and not force:
        print("skipped  mcgram already in `claude mcp list` (use --force to re-register)")
        return

    if already and force:
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            subprocess.run(
                ["claude", "mcp", "remove", "mcgram"],
                check=False, capture_output=True, timeout=10,
            )

    try:
        result = subprocess.run(
            [
                "claude", "mcp", "add",
                "--scope", "user", "mcgram",
                "--env", f"MCGRAM_CONFIG={cfg}",
                "--", "mcgram",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as e:
        print(f"skip     `claude mcp add` failed: {e}")
        return

    if result.returncode == 0:
        print("registered  mcgram with Claude Code (scope: user)")
    else:
        print(f"skip        `claude mcp add` exited {result.returncode}")
        if result.stderr:
            print(f"            stderr: {result.stderr.strip()[:200]}")


def init_config(*, force: bool = False) -> int:
    """Scaffold ~/.mcgram/, install skill, register MCP. Idempotent."""
    home = Path.home() / ".mcgram"
    home.mkdir(parents=True, exist_ok=True)
    cfg = home / "config.yaml"
    env = home / ".env"

    created: list[str] = []
    skipped: list[str] = []

    if not cfg.exists() or force:
        cfg.write_text(_bundled_config_yaml(), encoding="utf-8")
        created.append(str(cfg))
    else:
        skipped.append(str(cfg))

    if not env.exists() or force:
        env.write_text(_ENV_TEMPLATE, encoding="utf-8")
        created.append(str(env))
    else:
        skipped.append(str(env))

    for p in created:
        print(f"created  {p}")
    for p in skipped:
        print(f"skipped  {p} (already exists; use --force to overwrite)")

    install_skill(quiet=False, force=force)
    _register_with_claude_code(cfg, force=force)

    print(_NEXT_STEPS.format(env=env, cfg=cfg))
    return 0
