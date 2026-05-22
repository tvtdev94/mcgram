---
phase: 4
title: CLI & Companion Skill
status: completed
priority: P2
effort: 1d
dependencies:
  - 2
---

# Phase 4: CLI & Companion Skill

## Overview

Build the `mcgram` CLI (`init`, `doctor`, `audit`, `install-skill`, `--version`) and the companion Claude Code skill installed at `~/.claude/skills/mcgram/SKILL.md` that teaches Claude when to call each tool. Mirrors `dbread init` / `dbread audit` / `dbread install-skill` UX exactly.

## Context Links

- Brainstorm: [`../20260521-brainstorm-mcgram-telegram-mcp/brainstorm-report.md`](../20260521-brainstorm-mcgram-telegram-mcp/brainstorm-report.md) §4 (CLI commands), §4 (companion skill)
- Reference: `C:\w\dbread\src\dbread\cli.py`, `C:\w\dbread\src\dbread\audit_cli.py`, `C:\w\dbread\src\dbread\skill_installer.py`

## Requirements

**Functional**
- `mcgram` (no args) → runs MCP stdio server (Phase 2 entry)
- `mcgram --version` → prints version
- `mcgram init [--force]` →
  - Creates `~/.mcgram/` if missing
  - Copies `config.example.yaml` → `~/.mcgram/config.yaml` (skip if exists, unless `--force`)
  - Creates `~/.mcgram/.env` with `MCGRAM_BOT_TOKEN=` stub
  - Installs companion skill to `~/.claude/skills/mcgram/SKILL.md`
  - Prints **how to get bot token + chat_id** (BotFather steps)
  - Prints exact `claude mcp add` command to paste
- `mcgram doctor` →
  - Loads config; reports missing/invalid
  - Resolves token from env; calls `getMe`; reports bot username
  - Reports `operator_chat_id` resolved
  - Sends test message `🩺 mcgram doctor: connection OK`
  - Prints summary table (each check + status)
  - Exit code 0 if all pass, 1 if any fail
- `mcgram audit` (analyzer) — mirror `dbread audit`:
  - `--since 1h|30m|2d` filter
  - `--tool send_message` filter
  - `--rejected` only rejections
  - `--tail` follow new lines
  - Default: summary counts + top failures
- `mcgram install-skill [--force]` — copy bundled `SKILL.md` to `~/.claude/skills/mcgram/SKILL.md`

**Non-functional**
- CLI starts in <500ms (no MCP/httpx imports at top of cli.py — lazy import)
- Cross-platform paths (use `pathlib`)
- `mcgram init` outputs Vietnamese-friendly bot-token instructions (English text with `(VI: ...)` hint)

## Architecture

```
src/mcgram/
├── cli.py                  # argparse dispatcher (~150 LOC)
├── cli_init.py             # init logic
├── cli_doctor.py           # doctor logic
├── cli_audit.py            # audit analyzer (port from dbread)
├── skill_installer.py      # copy bundled SKILL.md to ~/.claude/skills/mcgram/
└── data/
    ├── config.example.yaml
    └── skill/
        └── SKILL.md        # companion Claude skill, bundled in wheel
```

### `mcgram init` output (sample)

```
✓ Created ~/.mcgram/config.yaml
✓ Created ~/.mcgram/.env (edit MCGRAM_BOT_TOKEN)
✓ Installed Claude skill → ~/.claude/skills/mcgram/SKILL.md

Next steps:
  1. Get a bot token from @BotFather on Telegram (/newbot)
  2. Get your chat ID:
       - Open Telegram, /start your new bot
       - Visit: https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
       - Copy `chat.id` from the JSON
  3. Edit ~/.mcgram/.env  → paste MCGRAM_BOT_TOKEN
  4. Edit ~/.mcgram/config.yaml → set operator_chat_id
  5. Register with Claude Code:

     claude mcp add --scope user mcgram \
       --env MCGRAM_CONFIG=~/.mcgram/config.yaml \
       -- mcgram

  6. Restart Claude Code → /mcp → mcgram appears
  7. Test:  mcgram doctor
```

### Companion `SKILL.md` outline

```markdown
---
name: mcgram
description: Telegram bridge for Claude Code — send notifications, ask short questions, set reminders via personal Telegram bot.
---

# mcgram — Telegram bridge

Use this skill when the user says: "báo telegram khi xong", "tell me on Telegram when done",
"nhắc tôi", "remind me in X minutes", "hỏi tôi confirm trước khi", "ask me before deploying".

## When to use which tool

| User intent | Tool | Example |
|---|---|---|
| "Báo khi xong" / "ping me when done" | `send_message` | `send_message(text="✅ Build passed in 3m12s")` |
| "Gửi log lỗi" / "send the failing log" | `send_file` | `send_file(path="./logs/test-failures.log", caption="Failing tests")` |
| "Confirm trước khi deploy" / "ask me before X" | `ask` with 2 options | `ask(question="Deploy to prod?", options=["Approve","Cancel"], timeout_s=300)` |
| "Chọn giúp tôi" / "pick one" | `ask` with options | `ask(question="Which DB to migrate?", options=["staging","prod"])` |
| "Hỏi tôi tự do" / "open-ended question" | `ask` without options | `ask(question="Any special instructions?")` |
| "Nhắc sau 30 phút" / "remind me in 30 min" | `set_reminder` | `set_reminder(text="Check container logs", delay_s=1800)` |

## Defaults & limits

- `ask` timeout default 120s, max 600s. Keep timeouts short — they block the Claude Code session.
- `send_file` size cap 50 MB. Paths must be inside CWD (or config opt-in `allow_outside_cwd: true`).
- Max 10 pending reminders. Max delay 24h. Reminders LOST on Claude Code shutdown — don't promise long-term reminders.
- 6 buttons max per `ask`. Use freetext for open-ended.

## Recovery

- `rate_limit_exceeded` → wait 60s, retry.
- `polling_conflict_lock_held` → another mcgram instance running. Close other Claude Code session.
- `ask` returning `source: timeout` → user didn't reply. Decide: retry, fall back to default, or abort.

## Audit

User can inspect activity with `mcgram audit`. Don't include bot token or sensitive payloads in messages
unless they explicitly asked.
```

## Related Code Files

- **Create:**
  - `src/mcgram/cli.py`
  - `src/mcgram/cli_init.py`
  - `src/mcgram/cli_doctor.py`
  - `src/mcgram/cli_audit.py`
  - `src/mcgram/skill_installer.py`
  - `src/mcgram/data/config.example.yaml`
  - `src/mcgram/data/skill/SKILL.md`
- **Modify:**
  - `pyproject.toml` — add `package-data` / `include` for `data/**`
  - `src/mcgram/server.py` — invoked when `cli.main()` runs with no subcommand

## Implementation Steps

1. **cli.py** — argparse with subparsers: `init`, `doctor`, `audit`, `install-skill`. Default (no subcommand) → import `server.run()` lazily and exec.
2. **cli_init.py** — implement scaffold + skill install + print instructions. Use `importlib.resources.files("mcgram.data")` for bundled templates.
3. **cli_doctor.py** — async check sequence: config load → token resolve → `client.get_me()` → `client.send_message(operator_chat_id, "🩺 ...")`. Pretty-print table.
4. **cli_audit.py** — port from `C:\w\dbread\src\dbread\audit_cli.py` adjusting record schema (we have `tool` not `conn`, `text_len` not `rows`).
5. **skill_installer.py** — function `install_skill(force: bool) -> Path` that copies bundled `SKILL.md` to `~/.claude/skills/mcgram/SKILL.md`.
6. **SKILL.md** — write the teaching content per outline above.
7. **pyproject.toml** — declare `package-data` so `data/` ships in wheel.
8. **Manual smoke**:
   - `pipx install ./dist/*.whl` → `mcgram --version` works
   - `mcgram init` produces files + prints instructions
   - `mcgram doctor` with valid creds → all green, test message received
   - `mcgram doctor` with bad token → fails with clear message
   - `mcgram audit --tail` follows live audit writes
   - Skill file at `~/.claude/skills/mcgram/SKILL.md` exists and is loadable

## Success Criteria

- [ ] `mcgram --help` lists all 4 subcommands
- [ ] `mcgram init` is idempotent (re-run without `--force` doesn't clobber)
- [ ] `mcgram init --force` clobbers existing files with confirmation
- [ ] `mcgram doctor` reports each check with ✓/✗ and exits non-zero on any failure
- [ ] `mcgram audit` accepts all dbread-equivalent flags
- [ ] Companion skill loads in Claude Code (verify by `/skills` showing `mcgram`)
- [ ] CLI startup latency <500ms (`time mcgram --version`)
- [ ] All new modules < 200 LOC

## Risk Assessment

- **`importlib.resources` API drift**: use the new `files()` API (Python 3.9+), avoid deprecated `read_text`.
- **Skill install path on Windows**: `~/.claude/` resolves via `Path.home()`; verify works under user profile with spaces.
- **Doctor sends real message**: confirm the user is OK with this (the message itself documents that the test ran). Acceptable.
- **CLI startup time**: lazy-import `mcp` SDK + `httpx`; only `server.run()` triggers them.

## Security Considerations

- `mcgram init` never displays the bot token in stdout
- `mcgram doctor` shows bot username + masked token (last 4 chars only)
- `.env` written with permissive default; recommend `chmod 600` (best-effort on Windows)

## Next Steps

Phase 5 — tests + docs + CI complete the product.
