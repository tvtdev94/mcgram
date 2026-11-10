# Security threat model (STRIDE)

> Honest about what mcgram defends against — and what it does not.

mcgram is a **personal** tool: one user, one bot, one chat, running on a developer's own machine. The threat model is calibrated to that scope.

## Trust boundaries

```
[ Claude Code ] ── stdio ── [ mcgram process ] ── HTTPS ── [ Telegram Bot API ]
                                    │              ├── [ ntfy.sh API ]
                                    │              └── [ Discord Webhooks ]
                                    │
                                    └── ~/.mcgram/{config.yaml, .env, audit.jsonl, .lock, spool/}
```

`spool/` is the one artifact where **write** access is a decision-making
capability: with the broker on, a file in `spool/asks/` can stand in for the
operator's answer. See [Broker spool](#broker-spool).

- **In trust boundary:** the mcgram process, its config dir (`~/.mcgram/`), the local Claude Code session.
- **Outside:** Telegram's servers, Discord's servers, ntfy.sh, the public internet, anyone who could intercept TLS or compromise credentials.
- **Adversary:** prompt injection inside Claude's context that tries to abuse the tools; a stolen bot token or Discord webhook URL; someone who guesses the bot username and DMs it.

## STRIDE

### S — Spoofing

| Threat | Mitigation |
|---|---|
| Random Telegram user DMs the bot to fake an "operator reply" | `update_dispatcher.from_operator` rejects any update whose `chat.id != operator_chat_id`. Rejected updates audited as `{tool:"_polling", status:"rejected", reason:"non_operator"}`. |
| Prompt injection in Claude's context tells it to send credentials | Cannot defend in the bridge — Claude's prompt-handling owns this. Audit log + `redact_text` option let the user notice after the fact. |
| Second `mcgram` process picks up the same bot token (would steal updates) | `SingleInstanceLock` (PID file) elects a single poller per machine; losers run send-only and never call `getUpdates`. Also: Telegram returns HTTP 409 if two clients poll the same bot — `tg_client` surfaces it as `TelegramError`, and the loop backs off 10s. |
| Degraded instance answers an `ask` it can't actually receive (operator's reply is delivered elsewhere) | **Broker off (default):** a non-owner returns `polling_not_owned` immediately, and only the polling process holds pending questions. **Broker on:** a non-owner may wait for an answer that the polling process relays through the spool. That gives up the "only the poller can answer" property. What replaces it is a heartbeat check before anything is written and provenance binding on the answer — see [Broker spool](#broker-spool). |

### T — Tampering

| Threat | Mitigation |
|---|---|
| Local malware modifies `~/.mcgram/audit.jsonl` to hide its tracks | Out of scope — local code execution beats anything in user space. Audit is best-effort forensics, not tamper-proof. |
| Stolen Telegram bot token used elsewhere | Token lives in `~/.mcgram/.env`, never in stdout, never in audit, never in `config.yaml`. Recommend chmod 600 on Unix. Rotate via @BotFather → `/revoke` if compromised. |
| Stolen Discord webhook URL used elsewhere | Webhook URL (address + secret) lives in `~/.mcgram/.env` only, never in `config.yaml`, stdout, error messages, or audit. `config.yaml` stores only the env var name (e.g., `discord_webhook_env: DISCORD_EVE`). Audit logs only the numeric webhook ID (via `webhook_id_from_url`) + thread_id, never the full URL. Regenerate via Discord Server Settings → Integrations → Webhooks if compromised. |
| MITM on Telegram / Discord / ntfy API | httpx uses HTTPS with system trust store. Pinning is not implemented. |

### R — Repudiation

| Threat | Mitigation |
|---|---|
| User claims "Claude never asked me to deploy" | Every `ask` + every `send_message`/`send_file` written to `audit.jsonl` with `fsync` per line — survives `kill -9` and power loss. Plus the message itself in the Telegram chat history. |
| User claims their answer was lost | `audit.jsonl` records `{question_id, source, value_len, ms_to_resolve}` (value text always recorded — disable with `redact_text: true`). |

### I — Information disclosure

| Threat | Mitigation |
|---|---|
| Bot token leaks via logs / stdout / audit | Token never written to any output. `mcgram doctor` masks token (`***last4`). `mcgram init` instructions explicitly tell user to gitignore `.env`. |
| Discord webhook URL leaks via logs / stdout / audit | Webhook URL never written to any output. Audit logs only the numeric webhook ID (extracted via `webhook_id_from_url`, which never accesses the token part). `mcgram doctor` confirms webhook liveness but never echoes the URL. `mcgram channel list` shows only the env var name. Error messages from Discord API carry only Discord's message, not the webhook URL. |
| `mcgram channel add-discord` webhook URL appears in shell history | `--webhook <URL>` flag is visible in history. **Safer:** omit the flag and use the secure `getpass` prompt (piped input, not echoed). Recommended: `mcgram channel add-discord eve` (no flag) and enter the URL when prompted. |
| Prompt injection makes Claude `send_file(/etc/passwd)` or similar | `send_file` rejects paths outside CWD by default. Operator can opt-in `allow_outside_cwd: true` — explicitly documented as a trust trade-off. |
| Telegram-side: sent files are stored on Telegram servers | True for all bots. Don't send secrets. Use `send_message` for short notifications, not for forwarding production data. Audit redaction (`redact_text`) protects the local log but cannot un-send a Telegram message. |
| Discord-side: sent files are stored on Discord servers | True for Discord webhooks. Don't send secrets. |
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
- Compromised Discord server (webhook URL theft lets someone POST to your channel)
- Sophisticated traffic analysis of the user's home IP ↔ Telegram / Discord / ntfy servers
- Anyone with `~/.mcgram/` read access — they have your bot token + all Discord webhook URLs
- With the broker on, anyone who can **write** `~/.mcgram/spool/` as your user can forge an `ask` answer. Write access is not the same as having the token: the token lets you *send* as the bot, not fabricate an operator reply. Provenance binding raises the bar but does not stop a same-uid attacker.

## Broker spool

`~/.mcgram/spool/` holds two namespaces:

- `reminders/` is always on. It holds pending reminders so they survive a restart.
- `asks/` exists only with `broker.enabled: true`, which is off by default.

Dirs are 0700 and files 0600 on POSIX. At boot, `spool_safety.probe_dir` refuses a spool root or namespace dir that is a symlink, owned by another uid, or wider than 0700 (on Windows the user-profile ACLs carry this instead). Keys are validated against `[a-z]_[0-9a-f]{8,32}`, so ids like `../x`, `a/b` or `C:evil` never reach the filesystem. Every record has an `expires_at` and is swept, including orphans left by killed processes.

| STRIDE | Threat | Mitigation |
|---|---|---|
| **S** | Prompt injection with shell access writes `asks/q_x.answer.json = {"value":"Approve"}` and approves its own dangerous action | **Broker off by default.** When on, the requester accepts an answer only if `answered_by_pid` is a pid it saw holding the poll lock while it waited (an owner that shuts down answers `cancelled` and then releases the lock), and logs a warning otherwise. Both sides audit `via: broker` + `answered_by_pid`. This turns forgery from "write one file" into "hold the poll lock", but it does **not** stop a determined same-uid attacker, who can read the lock file too. Don't enable the broker where `ask` gates truly dangerous actions and untrusted code runs as your user. |
| **T** | Edit a pending `request.json` so the operator sees a different question, or write one aimed at another chat or past the limits | Files are 0600 in a 0700 dir with short TTLs. A same-uid process can still edit the question — same caveat as above. The owner holds every request to its **own** config: `chat_id` must be its `operator_chat_id`, options at most `ask_options_max`, question and labels at most 4096 characters, `timeout_s` at most `ask_timeout_max_s`. Otherwise it posts nothing and answers `invalid_request`, which the requester reports at once. |
| **T** | Plant a `reminders/r_x.rec.json` so mcgram sends attacker text to your chat | Same-uid only, given the probe. Records are validated (types, key match, text/delay caps) before adoption. The bot can only reach your own configured channels. |
| **I** | Question text and options are **on disk** while an ask is open; reminder text is on disk until it fires | It has to be: the owner reads the question to post it, and the requester reads `value` to return it. **`audit.redact_text` does not apply to the spool** — redacting there would break the feature. Mitigated by 0600, short TTLs, and sweeping. |
| **D** | Flood the spool with requests or orphan reminders | Owner-side rate limit (the bot token's budget) for brokered asks. Adoption stops at `reminder_max_pending` per process. Expired records are swept. |
| **D** | Owner in 409/backoff, dead, or on an older protocol leaves requesters blocking | Requesters check the heartbeat (fresh, same protocol, live pid) before writing anything and fail fast with `polling_not_owned`. A demoted owner withdraws its heartbeat at once. |
| **E** | Two pollers (`MCGRAM_SKIP_LOCK=1`) serve one request twice, or two instances fire one reminder twice | Requests are claimed with `taken` via `O_EXCL`. Orphan reminders are claimed per owner generation via `O_EXCL`, and a fired or cancelled reminder gets an `O_EXCL` tombstone before its record is removed, so a failed delete cannot make it fire again. Owners prove liveness with a heartbeat, not the pid alone, so a reused pid cannot strand or double-claim a reminder. A reminder can still fire twice if a process dies between sending and writing the tombstone. |

`broker.spool_dir` can move the spool; the probe still applies. NFS/SMB shares are unsupported (`os.replace` and `O_EXCL` are not reliably atomic there).

## Reporting issues

Open a GitHub issue. For security-sensitive reports, mark the issue **private** if your platform supports it, or open a generic "security concern" issue and request a private channel.
