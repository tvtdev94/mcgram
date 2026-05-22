# Brainstorm — `mcgram`: Telegram MCP for Claude Code

**Date:** 2026-05-21
**Author:** brainstorm session
**Status:** approved, ready for `/ck:plan`

---

## 1. Problem Statement

User wants Claude Code to talk to them via Telegram when they're away from the keyboard:
- One-way: send progress/completion reports, reminders
- Two-way (light): ask for approval / pick-an-option, wait for short reply
- NOT: full remote-control of Claude Code from phone

Style and packaging must mirror `C:\w\dbread` — `uv tool install`, `~/.<name>/config.yaml` + `.env`, CLI subcommands, companion Claude skill, modules <200 LOC, audit JSONL with rotation, full product (PyPI + GitHub + docs + CI).

---

## 2. Confirmed Requirements

| # | Requirement | Decision |
|---|---|---|
| R1 | Interaction scope | Report + short Q&A (approve/reject/pick option). No remote-control. |
| R2 | Always-on | No. Bot lives in MCP process; polls only while Claude Code session open. |
| R3 | Channel model | Single `operator_chat_id` (DM with bot). |
| R4 | Distribution | Full product: PyPI + GitHub public + docs + CI + companion skill. |
| R5 | Reminder semantics | In-session scheduled (`delay_s` up to 24h). Lost on MCP shutdown. |
| R6 | Auto-notify hooks | No. Only explicit MCP tool calls. |
| R7 | `ask` behavior | Inline buttons + timeout (max 600s). Free-text reply also accepted. |
| R8 | `send_file` | Yes in v1 (gửi log/screenshot khi báo cáo). |
| R9 | Docs language | English-only (match dbread). |

---

## 3. Approaches Evaluated

### A. Pure MCP server (single process) — **CHOSEN**
Bot polling lives inside MCP server process. Starts when Claude Code connects, stops when it disconnects.
- ✅ Simplest, matches dbread style exactly
- ✅ No daemon/service install
- ✅ Single binary, single config
- ⚠️ Cannot receive Telegram messages while CC closed (acceptable per R2)
- ⚠️ Two CC instances → polling 409 conflict → mitigated by PID lock

### B. Daemon + thin MCP client
Background daemon polls Telegram 24/7; MCP is HTTP/IPC client.
- ✅ Reminders survive across sessions
- ✅ Bot online when CC closed
- ❌ Windows service install friction
- ❌ Extra IPC layer, more files
- ❌ Rejected by R2

### C. Webhook on VPS / Cloudflare Workers
Bot deployed externally, local MCP pulls queue.
- ✅ 24/7 without local daemon
- ❌ Requires hosting + public endpoint
- ❌ Latency + cost
- ❌ Overkill for personal use
- ❌ Rejected by R2

---

## 4. Final Solution — Architecture

### Data flow
```
Claude Code ─stdio─▶ mcgram MCP server ─httpx─▶ Telegram Bot API
                          │
                          ├── server.py          MCP stdio entry, tool registry
                          ├── tools.py           6 tool handlers
                          ├── tg_client.py       thin Bot API wrapper + long-poll loop
                          ├── reminders.py       asyncio scheduler (in-process)
                          ├── ask_registry.py    pending Q&A → asyncio.Future map
                          ├── audit.py           JSONL + fsync + rotate (mirror dbread)
                          ├── config.py          pydantic Settings (YAML + env)
                          ├── cli.py             init / audit / doctor
                          └── lock.py            PID-file single-instance guard

Companion: ~/.claude/skills/mcgram/SKILL.md
```

Every runtime module stays under 200 LOC.

### MCP tools (6 total)

| Tool | Input | Output | Notes |
|---|---|---|---|
| `send_message` | `text`, `silent?`, `parse_mode?` | `{message_id}` | One-way notification. |
| `send_file` | `path`, `caption?`, `silent?` | `{message_id}` | ≤50MB cap, path traversal guard, audit redact path. |
| `ask` | `question`, `options[≤6]?`, `timeout_s?` (def 120, max 600) | `{value, source: button \| freetext \| timeout}` | Inline keyboard. Strip buttons after resolve. Free-text reply also resolves. |
| `set_reminder` | `text`, `delay_s` (max 86400) | `{reminder_id, fires_at}` | asyncio task; lost on shutdown. |
| `cancel_reminder` | `reminder_id` | `{ok: bool}` | |
| `list_reminders` | — | `[{id, text, fires_at}]` | |

### Security model (5 layers, scale-down of dbread)

| Layer | Mechanism | Blocks |
|---|---|---|
| **0** | Bot is personal-only (no group adds), token in `.env` | Token exposure, group leak |
| **1** | `operator_chat_id` allowlist on every update | Strangers spamming the bot |
| **2** | Per-tool rate limit (default 20 msg/min, global cap) | Runaway loops |
| **3** | Hard caps: `ask` timeout ≤600s, reminder delay ≤24h, file size ≤50MB, options ≤6 | Resource abuse / infinite block |
| **4** | JSONL audit (fsync, rotate at 25MB, optional `redact_text`) | Forensics, PII control |

### Config (mirror dbread)

```yaml
# ~/.mcgram/config.yaml
bot:
  token_env: MCGRAM_BOT_TOKEN
  operator_chat_id: 123456789

defaults:
  parse_mode: plain                 # plain | markdown_v2
  ask_timeout_s: 120
  rate_limit_per_min: 20

limits:
  ask_timeout_max_s: 600
  reminder_max_delay_s: 86400
  reminder_max_pending: 10
  file_max_bytes: 52428800          # 50 MB
  ask_options_max: 6

audit:
  path: ~/.mcgram/audit.jsonl
  rotate_mb: 25
  redact_text: false
```

```ini
# ~/.mcgram/.env
MCGRAM_BOT_TOKEN=123456:ABC...
```

### Tech stack

- `httpx` (async) — direct Bot API calls, no heavyweight client lib
- `asyncio` builtin — polling loop + reminder scheduler
- `mcp` SDK Python — stdio transport
- `pydantic` v2 — typed config
- `pyyaml` — config file
- **Not using** `python-telegram-bot` / `aiogram` — too heavy for 6 tools, fits dbread minimalism

### CLI commands

| Command | Purpose |
|---|---|
| `mcgram` | Run MCP server (stdio) — invoked by Claude Code |
| `mcgram init` | Scaffold `~/.mcgram/{config.yaml,.env}`, print `claude mcp add` line, install companion skill |
| `mcgram doctor` | Validate token, ping bot, send test message to operator_chat_id |
| `mcgram audit` | Analyze audit.jsonl (since/conn/slow/rejected/tail) — same UX as dbread audit |
| `mcgram install-skill --force` | Reinstall companion skill after upgrade |

### Companion Claude skill (`~/.claude/skills/mcgram/SKILL.md`)

Teaches Claude:
- When user says "báo Telegram khi xong" → call `send_message`
- When user says "hỏi confirm trước khi deploy" → call `ask` with `["Approve","Cancel"]`
- When user says "nhắc tôi sau 30 phút" → call `set_reminder(delay_s=1800)`
- When attaching log/screenshot → use `send_file`
- Audit log location, how to grep, how to recover from rate-limit

---

## 5. Risks & Mitigations

| Risk | Severity | Mitigation |
|---|:-:|---|
| `ask` blocks session indefinitely | High | Hard cap 600s. Doctor checks. Skill teaches "use short timeouts." |
| Two Claude Code windows → Telegram polling 409 | Med | PID lock at `~/.mcgram/.lock`. Second instance fails fast with clear message. |
| Telegram MarkdownV2 escape breakage | Med | Default `parse_mode: plain`. Opt-in markdown via tool arg. |
| User misses reply window → Claude takes default | Med | `ask` returns `source: timeout` explicitly; skill teaches handling. |
| Bot token in audit log | High | Never log token. Audit captures `text`, `chat_id`, `status`. `redact_text` for PII. |
| Path traversal via `send_file` | Med | Resolve absolute path, reject paths outside CWD by default; whitelist via config. |
| Reminders lost on crash | Low | Documented behavior per R5. Future v0.2 could add SQLite persist. |
| Telegram Bot API rate (30 msg/sec to different chats, 1/sec same chat) | Low | Per-tool rate limit lower than TG's. |

---

## 6. Success Criteria

1. `uv tool install mcgram` → `mcgram init` → paste output into Claude Code → restart → `/mcp` shows `mcgram` — total install time <2 minutes
2. "Khi fix xong bug X, báo Telegram" — Claude calls `send_message`, user receives within ~1s
3. "Trước khi deploy prod, hỏi tôi confirm" — bot sends `[Approve][Cancel]` inline; tap → Claude proceeds correctly; ignore → timeout default applied
4. "Nhắc tôi sau 30 phút check container" — Telegram ping arrives at +30min ±5s
5. "Gửi screenshot lỗi" — `send_file(path)` delivers attachment with caption
6. `mcgram audit --rejected` shows any blocked updates with reason
7. 80%+ test coverage; pytest + ruff + CI matrix (3.11/3.12 × Ubuntu/Windows) all green
8. Two CC instances → second fails with `polling_conflict_lock_held` (not silent crash)

---

## 7. Out of Scope (v1)

- Multi-channel / per-project routing (R3 → single chat)
- Always-on daemon, reminders across restarts (R2/R5)
- Webhook deployment, VPS hosting (Approach C rejected)
- Claude Code hooks integration (R6)
- Group chat / topic threading
- Inbound message reading without prior `ask` (no `read_inbox`)
- Edit/delete posted messages from Claude (YAGNI)
- i18n docs (R9)

---

## 8. Effort Estimate

| Phase | Effort |
|---|---|
| Scaffolding (pyproject, uv, structure, lock file) | 0.5 day |
| Core: tg_client, server, tools (send_message, send_file) | 1 day |
| `ask` flow + ask_registry + inline keyboard + freetext fallback | 1 day |
| Reminders (asyncio scheduler + cancel/list) | 0.5 day |
| Audit (mirror dbread audit.py) | 0.5 day |
| CLI: init / doctor / audit | 0.5 day |
| Companion skill SKILL.md | 0.5 day |
| Tests (pytest, ≥80% cov) | 1 day |
| README, docs/, CI matrix | 1 day |
| **Total** | **~6.5 days chỉn chu, ~3 days personal-grade** |

---

## 9. Dependencies / Next Steps

1. **User**: create Telegram bot via @BotFather, get token + chat_id (for `mcgram init` to consume)
2. **Plan**: handoff to `/ck:plan` — phased breakdown:
   - Phase 1: scaffolding + config + audit
   - Phase 2: tg_client + send_message + send_file
   - Phase 3: ask flow + reminders
   - Phase 4: CLI + skill
   - Phase 5: tests + docs + CI
3. **Reference repo**: `C:\w\dbread` for every style decision (file layout, audit format, CLI UX, README structure)

---

## 10. Unresolved Questions

None — all clarifying rounds answered. Ready to plan.
