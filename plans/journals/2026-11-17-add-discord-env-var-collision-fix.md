---
title: add-discord env var collision fix
date: 2026-11-17
summary: "Two Discord channel names could derive one env var and overwrite each other's webhook; add-discord now refuses"
---

# add-discord env var collision fix

## What happened
Issue #1 (external reporter): `_env_name_for` maps `team-a`/`team_a` and `eve`/`Eve` to the same
`MCGRAM_DISCORD_WEBHOOK_*` var; names with no ASCII letter or digit map to `MCGRAM_DISCORD_WEBHOOK_`.
`_add_discord` checked only channel names, and `upsert_env_var` replaces the existing key, so the
second add repointed the first channel's webhook. Reproduced in a temp dir with fake URLs.

## Decision
`src/mcgram/cli_channel.py`: `_env_var_owner` refuses an env var already used by another channel,
`bot.token_env` or `ntfy.access_token_env` (the channel itself is allowed on `--force`). An empty slug
requires `--env-name`. Both checks run before the network probe, so nothing is written or sent.
Chose an owner check over a `MCGRAM_DISCORD_WEBHOOK_` prefix rule to keep `--env-name MY_HOOK` working.

## Verification
6 new tests in `tests/test_cli_discord.py`; 5 failed on the old code. Full suite 623 passed; ruff
clean; mypy shows 37 errors both before and after the change. No live sends to real channels.

## Next steps
Commit, PR, close issue #1 when the user authorizes it.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
