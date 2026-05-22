"""`mcgram init` — scaffold ~/.mcgram/ and install companion skill."""

from __future__ import annotations

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
  5. Register with Claude Code:

       claude mcp add --scope user mcgram \\
         --env MCGRAM_CONFIG={cfg} \\
         -- mcgram

  6. Restart Claude Code → /mcp → mcgram appears
  7. Test:  mcgram doctor
"""


def init_config(*, force: bool = False) -> int:
    """Scaffold ~/.mcgram/{config.yaml, .env} and install the skill. Idempotent."""
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

    # Visible so the user knows the Claude Code skill is wired up.
    install_skill(quiet=False, force=force)

    print(_NEXT_STEPS.format(env=env, cfg=cfg))
    return 0
