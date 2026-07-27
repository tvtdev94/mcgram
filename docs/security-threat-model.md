# Security threat model (STRIDE)

> Honest about what mcgram defends against — and what it does not.

mcgram is a **personal** tool: one user, one bot, one chat, running on a developer's own machine. The threat model is calibrated to that scope.

## Trust boundaries

```
[ Claude Code ] ── stdio ── [ mcgram process ] ── HTTPS ── [ Telegram Bot API ] ── [ Operator's phone ]
                                    │
                                    └── ~/.mcgram/{config.yaml, .env, audit.jsonl, .lock}
```

- **In trust boundary:** the mcgram process, its config dir, the local Claude Code session.
- **Outside:** Telegram's servers, the public internet, anyone who could intercept TLS or compromise the bot account.
- **Adversary:** prompt injection inside Claude's context that tries to abuse the tools; a stolen bot token; someone who guesses the bot username and DMs it.

## STRIDE

### S — Spoofing

| Threat | Mitigation |
|---|---|
| Random Telegram user DMs the bot to fake an "operator reply" | `update_dispatcher.from_operator` rejects any update whose `chat.id != operator_chat_id`. Rejected updates audited as `{tool:"_polling", status:"rejected", reason:"non_operator"}`. |
| Prompt injection in Claude's context tells it to send credentials | Cannot defend in the bridge — Claude's prompt-handling owns this. Audit log + `redact_text` option let the user notice after the fact. |
| Second `mcgram` process picks up the same bot token (would steal updates) | `SingleInstanceLock` (PID file) elects a single poller per machine; losers run send-only and never call `getUpdates`. Also: Telegram returns HTTP 409 if two clients poll the same bot — `tg_client` surfaces it as `TelegramError`, and the loop backs off 10s. |
| Degraded instance answers an `ask` it can't actually receive (operator's reply is delivered elsewhere) | `ask` requires poll ownership and returns `polling_not_owned` immediately. The `AskRegistry` exists only in the polling process, so no non-owner can hold a pending question. |

### T — Tampering

| Threat | Mitigation |
|---|---|
| Local malware modifies `~/.mcgram/audit.jsonl` to hide its tracks | Out of scope — local code execution beats anything in user space. Audit is best-effort forensics, not tamper-proof. |
| Stolen bot token used elsewhere | Token lives in `~/.mcgram/.env`, never in stdout, never in audit. Recommend chmod 600 on Unix. Rotate via @BotFather → `/revoke` if compromised. |
| MITM on Telegram API | httpx uses HTTPS with system trust store. Pinning is not implemented. |

### R — Repudiation

| Threat | Mitigation |
|---|---|
| User claims "Claude never asked me to deploy" | Every `ask` + every `send_message`/`send_file` written to `audit.jsonl` with `fsync` per line — survives `kill -9` and power loss. Plus the message itself in the Telegram chat history. |
| User claims their answer was lost | `audit.jsonl` records `{question_id, source, value_len, ms_to_resolve}` (value text always recorded — disable with `redact_text: true`). |

### I — Information disclosure

| Threat | Mitigation |
|---|---|
| Bot token leaks via logs / stdout / audit | Token never written to any output. `mcgram doctor` masks token (`***last4`). `mcgram init` instructions explicitly tell user to gitignore `.env`. |
| Prompt injection makes Claude `send_file(/etc/passwd)` or similar | `send_file` rejects paths outside CWD by default. Operator can opt-in `allow_outside_cwd: true` — explicitly documented as a trust trade-off. |
| Telegram-side: sent files are stored on Telegram servers | True for all bots. Don't send secrets. Use `send_message` for short notifications, not for forwarding production data. Audit redaction (`redact_text`) protects the local log but cannot un-send a Telegram message. |
| Claude leaks operator chat ID into a public log | `chat_id` is logged in audit. It's not a secret per se (anyone who DMs the bot will see it). Treat it like a user ID. |

### D — Denial of service

| Threat | Mitigation |
|---|---|
| Prompt injection spams `send_message` 1000×/sec | Per-tool token bucket (default 20/min). 21st call rejected with `RateLimitError`. |
| Prompt injection schedules 1000 reminders for 24h later (memory exhaustion) | `reminder_max_pending` (default 10) + `reminder_max_delay_s` (24h) + `reminder_text_max_chars` (1000). |
| Prompt injection uses `ask(timeout_s=86400)` to freeze Claude forever | `limits.ask_timeout_max_s` (default 600s) hard-caps the wait. |
| Prompt injection uploads a 10 GB file | `limits.file_max_bytes` (default 50 MB — Telegram's hard limit anyway). |
| Telegram API returns persistent 5xx | Polling backs off exponentially (1s → 30s). Send tools fail-fast with `telegram_api` error and audit. |
| User runs several `mcgram` processes against the same bot (normal: one per Claude Code session) | The lock elects one poller; the rest run send-only and never poll. If bypassed via `MCGRAM_SKIP_LOCK=1`, Telegram returns 409 on `getUpdates` and the loop sleeps 10s per conflict instead of hot-looping. |
| Two processes rotate `audit.jsonl` at once and lose a backup | Rotation and pruning take a cross-process lock (`audit.jsonl.rotate.lock`); a loser skips rather than racing the renames. Appends use `O_APPEND` so concurrent writes can't clobber each other. Stale lock reclaimed after 60s. |

### E — Elevation of privilege

| Threat | Mitigation |
|---|---|
| Bot added to a group chat → group members can send commands | The dispatcher's operator filter rejects non-DM updates because group chat IDs differ from `operator_chat_id`. Still: don't add the bot to groups. |
| `send_file` path traversal (`../../etc/shadow`) | Path is `.resolve()`d to an absolute path; if `allow_outside_cwd: false`, `relative_to(cwd)` raises and rejects. |
| MCP `tools/call` with unexpected args crashes the server | All handlers wrap in `try/except` for `MCGramError`, `TypeError`, generic — return a structured `{error, reason}` instead of crashing the process. |

## What mcgram does NOT defend against

- Compromised local user account (anything in user space)
- Compromised Claude Code itself (it owns stdio)
- Compromised Telegram account (bot token theft has the same effect as a stolen password)
- Sophisticated traffic analysis of the user's home IP ↔ Telegram servers
- Anyone with `~/.mcgram/` read access — they have your bot token

## Reporting issues

Open a GitHub issue. For security-sensitive reports, mark the issue **private** if your platform supports it, or open a generic "security concern" issue and request a private channel.
