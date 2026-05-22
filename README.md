# mcgram

> Telegram bridge for Claude Code — Claude can ping you, ask you, and remind you via your personal Telegram bot.

[![CI](https://github.com/tvtdev94/mcgram/actions/workflows/ci.yml/badge.svg)](https://github.com/tvtdev94/mcgram/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mcgram.svg)](https://pypi.org/project/mcgram/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

mcgram is a Python MCP (Model Context Protocol) server. It plugs into Claude Code so the assistant can:

- 📣 **Send messages and files** to your personal Telegram bot when a task finishes
- ❓ **Ask short questions** with inline buttons and a timeout, blocking until you reply
- ⏰ **Set in-session reminders** ("nhắc tôi sau 30 phút check container logs")

Single process — the bot polling lives inside the MCP server. No daemon, no VPS, no webhook setup.

## Why

You're running long tasks in Claude Code. You want to walk away, get pinged when something finishes, and tap a button to approve the next step without staring at the terminal. mcgram makes that round-trip work — just you, your bot, and the MCP server running locally.

## Quickstart (2 minutes)

```bash
# 1. Install
uv tool install mcgram     # or: pipx install mcgram

# 2. Scaffold config + install the Claude Code skill
mcgram init

# 3. Create your bot with @BotFather on Telegram → /newbot
#    Copy the token, paste into ~/.mcgram/.env (MCGRAM_BOT_TOKEN=...)
#    Find your chat ID:
#      a) /start your bot
#      b) Visit  https://api.telegram.org/bot<TOKEN>/getUpdates
#      c) Copy chat.id into ~/.mcgram/config.yaml (operator_chat_id)

# 4. Register with Claude Code
claude mcp add --scope user mcgram \
  --env MCGRAM_CONFIG=~/.mcgram/config.yaml \
  -- mcgram

# 5. Verify
mcgram doctor   # → sends "🩺 mcgram doctor: connection OK" to your Telegram

# 6. Restart Claude Code → /mcp → mcgram appears → you're done
```

## Tools

| Tool | Purpose | Notes |
|---|---|---|
| `send_message` | Post text to the operator chat | ≤4096 chars, optional silent / markdown |
| `send_file` | Upload a local file as attachment | ≤50 MB, default CWD-only (opt-in `allow_outside_cwd`) |
| `ask` | Post a question, **block** until reply | Inline buttons OR freetext, default 120s timeout (max 600s) |
| `set_reminder` | Schedule a reminder | In-memory, lost on restart, ≤24h delay, ≤10 pending |
| `cancel_reminder` | Cancel a pending reminder | Returns `{ok}` |
| `list_reminders` | List pending reminders | Returns `[{id, text, fires_at}]` |

The companion Claude skill (`~/.claude/skills/mcgram/SKILL.md`, installed by `mcgram init`) teaches Claude when to call which tool — both English and Vietnamese trigger phrases are recognized.

## Config example

`~/.mcgram/config.yaml`:

```yaml
bot:
  token_env: MCGRAM_BOT_TOKEN     # token lives in ~/.mcgram/.env
  operator_chat_id: 123456789      # your personal chat ID

defaults:
  parse_mode: plain                # plain | markdown_v2
  ask_timeout_s: 120
  rate_limit_per_min: 20

limits:
  ask_timeout_max_s: 600
  reminder_max_delay_s: 86400      # 24h
  reminder_max_pending: 10
  file_max_bytes: 52428800         # 50 MB
  ask_options_max: 6

audit:
  path: ~/.mcgram/audit.jsonl
  rotate_mb: 25
  redact_text: false               # true: outbound `text` masked in audit
  retention_days: null             # e.g. 14
  timezone: UTC

allow_outside_cwd: false           # send_file restricted to CWD by default
```

## Security model

| Layer | Mitigation |
|---|---|
| Token | Loaded from env var; never logged; masked in `mcgram doctor` |
| Updates | All non-`operator_chat_id` updates rejected at dispatcher entry — never reach tool handlers |
| `send_file` | Path resolved + checked against CWD; size capped at `file_max_bytes` |
| Rate limit | Per-tool token bucket (default 20/min) |
| Single instance | PID-file lock at `~/.mcgram/.lock`; second `mcgram` exits with `LockHeldError` |
| Audit | Every call logged JSONL with `fsync`; survives `kill -9` |
| Reminder spam | Max 10 pending, 24h delay cap, 1000-char text cap |
| `ask` DoS | Hard timeout cap (600s) so a forgetful user can't freeze Claude forever |

See [docs/security-threat-model.md](docs/security-threat-model.md) for the full STRIDE analysis.

## Audit

```bash
mcgram audit                      # summary
mcgram audit --since 1h           # last hour only
mcgram audit --tool send_file     # filter by tool
mcgram audit --rejected           # only rejected calls, grouped by reason
mcgram audit --tail               # follow new entries (Ctrl-C to stop)
```

Audit lines look like:

```jsonc
{"ts":"2026-05-21T10:00:00+00:00","tool":"send_message","status":"ok","chat_id":123,"text":"build passed","text_len":12,"ms":150}
{"ts":"2026-05-21T10:00:05+00:00","tool":"ask","status":"ok","question_id":"q_a1","source":"button","ms":3200}
{"ts":"2026-05-21T10:00:10+00:00","tool":"send_file","status":"rejected","reason":"file_too_large","bytes":62914560}
```

## Known limitations

- **No persistent reminders** — schedules live in process memory. Restart = lost.
- **Single chat** — one operator chat, not multi-user. Personal use only.
- **`ask` blocks the MCP call** — keep timeouts short or Claude waits.
- **No remote control** — Claude can send, but the bot does not accept arbitrary commands from Telegram. (Future v0.2.)
- **No webhook / VPS support** — long-poll only.
- **No i18n in docs** — English only; the skill recognizes Vietnamese phrases.

## Architecture

```
Claude Code ↔ MCP stdio ↔ mcgram server ↔ httpx ↔ Telegram Bot API
                                ↓
                            long-poll loop ─── operator_chat_id filter ─── ask + reminder handlers
                                ↓
                          ~/.mcgram/audit.jsonl
```

Full module diagram + data flow: [docs/architecture.md](docs/architecture.md).

## Update

```bash
uv tool upgrade mcgram        # or: pipx upgrade mcgram
mcgram install-skill --force  # refresh the bundled Claude skill if changed
```

## License

MIT — see [LICENSE](LICENSE).
