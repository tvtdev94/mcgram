# Architecture

> One poller, one bot, one operator chat. Long-poll on the side, MCP stdio in front.
>
> Several mcgram processes may run at once (one per Claude Code session). They
> all serve MCP and all send; exactly one owns the Telegram long-poll. See
> [Poll ownership](#poll-ownership).

## Module map

```
src/mcgram/
├── cli.py                  # argparse dispatcher (lazy imports for <500ms)
├── cli_init.py             # `mcgram init` — scaffold + install skill
├── cli_doctor.py           # `mcgram doctor` — config + connectivity checks
├── cli_audit.py            # `mcgram audit` — JSONL analyzer
├── cli_channel.py          # `mcgram channel` — add/list Discord/Telegram/ntfy
├── server.py               # MCP stdio bootstrap; wires polling + tools
│
├── config.py               # pydantic Settings (YAML + .env)
├── lock.py                 # SingleInstanceLock (PID file, Windows + POSIX)
├── poll_ownership.py       # scopes the lock to polling; degraded mode + takeover
├── errors.py               # exception hierarchy
├── audit.py                # JSONL writer + rotation + retention + redact
│
├── tg_client.py            # httpx async wrapper around Telegram Bot API
├── ntfy_client.py          # httpx async wrapper around ntfy.sh
├── discord_client.py       # httpx async wrapper around Discord webhooks
├── update_dispatcher.py    # fan-out + operator allowlist (Telegram only)
├── polling.py              # long-poll loop with exponential backoff (Telegram only)
├── rate_limiter.py         # per-tool token bucket
├── runtime.py              # AppState dataclass
│
├── ask_registry.py         # PendingAsk + button/multi-select/freetext/timeout resolver
├── reminders.py            # asyncio.Task scheduler; saves/reloads/adopts via reminder_spool
├── skill_installer.py      # bundled Claude skill installer
│
├── spool.py                # atomic JSON records under ~/.mcgram/spool/<namespace>/, TTL sweep
├── spool_safety.py         # spool dir probe: no symlinks, owned by us, mode 0700 (POSIX)
├── ask_broker.py           # broker, requester side: heartbeat check, request, wait for answer
├── ask_server.py           # broker, owner side: claim requests, serve via AskRegistry.open
├── ask_via_broker.py       # `ask` from a non-polling session (never touches Telegram)
├── broker_wiring.py        # boot wiring: ask broker supervisor + reminder store
├── reminder_spool.py       # reminder records on disk: publish, list, cancel, adopt orphans
│
├── tools/
│   ├── send_message.py
│   ├── send_file.py
│   ├── send_video.py
│   ├── ask.py
│   ├── _ask_refusal.py     # why `ask` can't run here (fail-fast refusals)
│   ├── set_reminder.py
│   ├── cancel_reminder.py
│   └── list_reminders.py
│
└── data/                   # bundled in wheel
    ├── config.example.yaml
    └── skill/SKILL.md
```

Most modules stay under 200 lines; a handful (`config.py` and
`cli_channel.py` are the largest) run longer.

## Transports

mcgram supports three transports, each with different capabilities and constraints:

### Telegram (polling, two-way)
- Requires a bot token (`MCGRAM_BOT_TOKEN`), stored in `~/.mcgram/.env`
- Long-polls `getUpdates` to receive inbound messages (one client per bot token per machine)
- Supports `ask` (operator replies directly to the message button or freetext field)
- Tools: `send_message`, `send_file`, `ask`, `set_reminder` (all use the inbound channel)
- Enabled by `bot:` section in config.yaml

### ntfy.sh (one-way, no registration)
- No credentials needed (ntfy topics are unguessable UUIDs or password-protected)
- Send-only via HTTP POST to a public service
- Fallback for polling-disabled deployments
- Enabled by `ntfy:` section in config.yaml

### Discord (one-way, webhook-only)
- Webhook URL = address + secret (the full URL itself is the credential), stored in `~/.mcgram/.env`
- Send-only via HTTP POST to the webhook URL
- **No polling**, no `ask` support — returns `transport_unsupported` immediately
- Optional thread targeting: `thread_id` is passed at call time (query param, not body), never stored on the Destination
- One webhook URL per Discord channel = one mcgram destination
- Global `discord:` section holds display identity only (username, optional avatar_url)
- Enabled by defining channels with `transport: discord` in config.yaml

## Client construction

`server._build_clients` yields a 3-tuple `(tg_client, ntfy_client, discord_client)` where:
- `tg_client` is `TelegramClient | None` (None if no bot section)
- `ntfy_client` is `NtfyClient | None` (None if no ntfy section)
- `discord_client` is `DiscordClient | None` (None if no Discord channels)

Unlike TelegramClient and NtfyClient, which take credentials at init (`__init__`), DiscordClient is credential-agnostic — the webhook URL is passed to each method call (`send_message(webhook_url, ...)`, `send_file(webhook_url, ...)`). This design lets one client instance serve many Discord webhook URLs.

## Lifecycle

```mermaid
sequenceDiagram
    participant CC as Claude Code
    participant CLI as mcgram CLI
    participant SRV as MCP stdio server
    participant POLL as poll_loop (asyncio.Task)
    participant TG as Telegram API
    participant USR as Operator (Telegram)

    CC->>CLI: spawn `mcgram` over stdio
    CLI->>SRV: load config, open httpx client
    SRV->>POLL: create_task(poll supervisor) — polls only if it wins the lock
    SRV-->>CC: MCP `initialize` reply (never blocked by the lock)
    POLL->>TG: getUpdates(timeout=25)  (long-poll)

    CC->>SRV: tools/call send_message
    SRV->>TG: POST sendMessage
    TG-->>SRV: {ok: true, message_id}
    SRV-->>CC: {ok: true, message_id}

    Note over CC,SRV: `ask` blocks until reply

    CC->>SRV: tools/call ask(question, options)
    SRV->>TG: sendMessage(reply_markup=inline_keyboard)
    USR->>TG: tap button
    TG-->>POLL: callback_query update
    POLL->>SRV: dispatcher → ask_registry.handle_update
    SRV->>TG: answerCallbackQuery + editMessageReplyMarkup
    SRV-->>CC: {value, source: "button"}

    CC->>SRV: shutdown
    SRV->>POLL: task.cancel()
    SRV->>SRV: reminders.shutdown() (cancel tasks; saved records stay on disk)
    SRV-->>CC: stdout closes
```

## Data flow: an `ask` call

1. Claude Code → `tools/call ask(question="Deploy?", options=["Yes","No"], timeout_s=60)`
2. `tools/ask.py::handle` validates input + acquires a rate-limit token
3. `AskRegistry.open()` posts the question with inline keyboard, registers a `PendingAsk` keyed by `question_id`, awaits its `asyncio.Future` with `asyncio.wait_for(timeout_s)`
4. Operator taps a button → Telegram sends `callback_query` → polling loop receives it → `from_operator` filter passes (chat matches `operator_chat_id`) → dispatcher calls `AskRegistry.handle_update`
5. Handler looks up `q_<id>`, calls `future.set_result((value, "button"))`, sends `answerCallbackQuery` to clear the spinner
6. `asyncio.wait_for` resolves, registry edits the message to strip the keyboard, returns `{value, source: "button", question_id}`. On timeout the message gets `⏱ timed out` and also loses its keyboard. With `multi_select`, taps toggle a ✅ and redraw the keyboard; the future resolves only on **Done**, and `value` is the list of selected labels. `columns` (1–4) sets the buttons per row.
7. Audit log gets `{tool:"ask", status:"ok", source:"button", ms_to_resolve}`
8. Claude Code receives the structured response

## Data flow: `send_file`

1. `tools/send_file.py::handle` resolves path, rejects if non-existent / not regular file / size > cap / outside CWD (unless `allow_outside_cwd`)
2. Rate-limit token
3. Transport-specific dispatch:
   - **Telegram:** `httpx` multipart POST to `sendDocument`
   - **Discord:** `httpx` multipart `multipart/form-data` with `payload_json` + `files[0]` (query params include `wait=true` and optional `thread_id`)
4. Audit `{tool:"send_file", status:"ok", bytes, ms, message_id, [discord_webhook_id, thread_id]}`

## Operator filter

Every incoming *Telegram* update is checked at the dispatcher layer (`update_dispatcher.from_operator`). Non-operator updates are dropped + audited as `{tool:"_polling", status:"rejected", reason:"non_operator"}`. Tool handlers never see them.

This is the only line of defence against someone messaging the bot directly — Telegram bots have no built-in restriction. Keep `operator_chat_id` private and the bot's username unpublished.

**Discord has no inbound updates** — it is send-only. The webhook URL is unidirectional (POST into Discord), so there is no channel to receive replies.

## Polling resilience

Exponential backoff on errors: `1s → 2s → 4s → … → 30s`. Reset to `1s` on the first successful response. Errors are logged to audit as `{tool:"_polling", status:"error", error:str}`. The loop runs until `task.cancel()` (server shutdown).

HTTP 409 gets its own path: it means another client is polling this bot token, so the loop sleeps a flat 10s rather than escalating, and audits `{status:"conflict", reason:"409_another_poller"}`. Under normal operation the poll lock prevents 409 between local instances; it still appears with `MCGRAM_SKIP_LOCK=1` or the same token on a second machine.

## Poll ownership

Telegram's `getUpdates` accepts one client per bot token. Everything else mcgram does — sends, reminders, all of ntfy, all of Discord — is one-way HTTP and is safe from any number of processes at once.

So `~/.mcgram/.lock` guards **the poll loop**, not the process. Before v0.3.0 it wrapped the whole server, and a second Claude Code session died at the MCP handshake with `-32000`.

`poll_ownership.PollOwnership` owns this:

- **Acquire on start.** Won → full mode. Lost → send-only degraded mode; the server still boots and serves.
- **`ask` is gated on ownership.** The `AskRegistry` is attached to `AppState` only while this process polls. Without the broker (the default), a non-owner returns `polling_not_owned` (with the owning pid) *immediately*, because the operator's reply is delivered to the polling process — waiting could only end in a `timeout` result that misreports an unanswerable question as an ignored one. With the broker, the non-owner hands the question to the owner instead — and still fails fast when no live owner is there to serve it (see [Ask broker](#ask-broker)).
- **Takeover.** A degraded instance re-checks every 30s (`MCGRAM_POLL_RETRY_S`), so it picks up polling when the owner exits — including `kill -9`, where the stale lock is reclaimed by PID liveness check.
- **No lock when there's nothing to poll.** ntfy-only, Discord-only, or `bot.disable_polling: true` instances skip the lock entirely; taking it would starve a real Telegram instance.

Degraded mode is *not* implemented by flipping `bot.disable_polling` at runtime — that field also decides whether `channels.default` resolves to telegram or ntfy, so mutating it would silently reroute every send.

Discord channels do not affect polling because Discord is one-way only. A deployment with only Discord channels and no Telegram bot will not start a poller, will not take the lock, and will not return `polling_not_owned` for `ask` calls (instead returning `transport_unsupported` because Discord has no inbound channel).

## Concurrent audit writers

Several processes share one `audit.jsonl`. Appends use explicit `O_APPEND` so the kernel resolves the offset at write time and lines can't clobber each other. Rotation and retention pruning rewrite files, so they take a short cross-process lock (`audit.jsonl.rotate.lock`); a loser skips the attempt rather than racing the `.jsonl → .1 → .2 → .3` renames, and a lock left by a killed process is reclaimed after 60s.

## Ask broker

Opt-in (`broker.enabled: false` by default — read the
[threat model](security-threat-model.md#broker-spool) first). It lets a session
that does not poll Telegram use `ask`. The design is **owner-does-everything**:
the non-owner never talks to Telegram. It writes a request file and waits, and
the polling session serves the request through the unchanged
`AskRegistry.open()`, so a brokered ask is an ordinary pending ask there.

```
~/.mcgram/spool/asks/
├── u0.heartbeat.json   owner, every tick: {pid, at, protocol, expires_at}
├── q_x.request.json    requester: {question, options, multi_select, columns,
│                        chat_id, timeout_s, requester_pid, expires_at}
├── q_x.taken.json      owner, write_exclusive (O_EXCL) — a request is served once
└── q_x.answer.json     owner: {value, source, answered_by_pid, expires_at}
```

Requester (`ask_via_broker`):
1. Is the heartbeat fresh (< `heartbeat_stale_s`), the same protocol, and its pid alive? If not, return `polling_not_owned` with a `broker` reason right away. Nothing is posted or written.
2. Only `operator_chat_id` can answer (the poll loop drops other chats), so any other channel returns `ask_requires_operator_chat`.
3. Write the request (`write_exclusive`). If the write fails, return `broker_unavailable` before anything is posted.
4. Poll for the answer every `poll_interval_s`, with one final read after the deadline. Reject answers whose `answered_by_pid` did not hold the poll lock at some point during the wait.
5. Always remove the request and answer files. The owner's `taken` claim stays until it expires with the request (the sweep drops it), so a request whose file could not be deleted — on Windows a concurrent reader makes the unlink fail — is never served twice. An owner-side failure (`rate_limited`, `telegram_error`, `cancelled`, …) is returned as an error, never as a timeout.

Owner (`broker_wiring.serve_while_owner`): a supervisor task watches `state.ask_registry` and runs an `AskServer` exactly while this process holds the poll lock. On demotion or shutdown it first cancels the requests in flight: each answers `cancelled` while the poll lock is still held (shutdown stops the broker before it releases the lock), and the posted question is marked stopped and loses its buttons. Then it withdraws its heartbeat, so requesters fail fast instead of waiting out `heartbeat_stale_s`. The server claims each request with `taken`, spends the **owner's** rate-limit budget (one bot token, one budget), and writes an answer on every path. Serving happens in a background task, never inside the update dispatcher. The same supervisor sweeps expired records while not owning, so cleanup also runs where nobody polls.

## Reminder scheduler

`asyncio.create_task(asyncio.sleep(delay) → send_text(⏰ text))`, tracked in two dicts (`_tasks`, `_meta`) keyed by `r_<hex>`. Delivery goes through `dispatch.send_text`, so any transport works.

**Durable — independent of `broker.enabled`.** Each pending reminder is also saved as `~/.mcgram/spool/reminders/r_x.rec.json` (`reminder_spool`), with the owner's pid and a per-process instance token. It is removed when the reminder fires or is cancelled. Shutdown cancels the tasks but **keeps** the records. Every instance runs a watch task that does three things:

- **Owner liveness**: an owner counts as alive only while its pid runs **and** its heartbeat `o_<token>.alive.json` is fresher than `heartbeat_stale_s` (refreshed every watcher tick). A reused pid alone never keeps a dead owner's reminders hostage. A silent owner (pid alive, heartbeat stale) is adopted only after two consecutive adoption passes, and an owner re-checks that it still owns a reminder right before firing (audit `_reminder_fire` status `skipped` otherwise).
- **Adopt orphans** (on start, then every 5s): a record whose owner is not alive gets claimed with `write_exclusive` on `claim<dead-owner-token>`, so exactly one instance wins each generation. The winner rewrites the record as its own and schedules it. Overdue reminders fire at once, with a late note. Reminders cancelled while their owner was down are dropped, not fired. Adoption respects `reminder_max_pending` and the delay/text caps, and skips channels this instance cannot deliver to.
- **Tombstones**: a fired or cancelled reminder gets `r_x.done.json`, created with `write_exclusive` **before** the record is deleted. No instance fires or adopts a reminder that has one, and the owner retries a failed record delete every tick (on Windows a concurrent reader can make the unlink fail). `cancel_reminder` from any session — broker on or off — writes the tombstone, so the cancel reaches whoever owns or later adopts the reminder. It lives until the record's own expiry.
- **Sweep** expired records. A record expires 24h after its due time, so a reminder that came due during a longer outage is dropped unfired.

Delivery is at most once, except when a process is killed between sending and writing the tombstone; the adopter then fires it again. `list_reminders` also shows other sessions' pending records, with `local`, `owner_pid`, `owner_alive`, whenever the reminder store is usable (broker on or off). Fired and cancelled reminders are hidden.

## What lives outside this repo

- `~/.mcgram/config.yaml` — user config (channel names, chat IDs, ntfy topics; does NOT hold credentials)
- `~/.mcgram/.env` — bot token + Discord webhook URLs (credentials only; never version-controlled)
  - Format: `MCGRAM_BOT_TOKEN=<token>` + `DISCORD_<CHANNEL_NAME>=<webhook_url>` (env var names are arbitrary, set via `mcgram channel add-discord`)
- `~/.mcgram/.lock` — single-instance PID file
- `~/.mcgram/spool/` — `reminders/` (pending reminders, always) and `asks/` (only with `broker.enabled`); dirs 0700, files 0600 on POSIX. Anchored to the home directory, not to `audit.path`; overridable with `broker.spool_dir`. Local disk only — NFS/SMB shares are unsupported
- `~/.mcgram/audit.jsonl[.1..3]` — append-only audit log + rotated backups (redacted if `redact_text: true`)
- `~/.claude/skills/mcgram/SKILL.md` — companion skill, installed by `mcgram init`
