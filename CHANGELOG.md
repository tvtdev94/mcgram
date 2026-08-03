# Changelog

All notable changes to mcgram will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Discord @mentions** — `send_message` / `send_file` / `send_video` accept an
  optional `mention=["<name>"]` to ping registered users. Names resolve to
  `<@id>` tokens prepended to the message/caption; an unregistered name returns
  `unknown_mention` (with the `known` list) before any network call. Ignored on
  Telegram/ntfy (response carries a `note`). The mention prefix counts toward the
  2000-character Discord content cap.
- `discord.mentions` config map (`name -> Discord user id`) and CLI
  `mcgram discord mention add|list|remove` to manage it. User IDs live in
  `config.yaml` (not secrets, unlike webhook URLs).

### Changed
- **Discord messages now always send `allowed_mentions`.** Without an explicit
  `mention`, the baseline is `{"parse": []}` — `@everyone`/`@here`/role mentions
  that appear in raw message text no longer ping (safety hardening). Only user
  IDs resolved from the `mention` registry are whitelisted. Previously, raw
  mention text in `send_message`/caption could ping; that no longer happens.

## [0.4.0] — 2026-08-01

### Added
- **Discord webhook transport** — one-way push to Discord channels via incoming
  webhooks. Each webhook maps to one mcgram channel; declare as many as needed.
  `ask` is not supported (Discord webhooks have no reply path).
- `DiscordClient` async HTTP wrapper (`src/mcgram/discord_client.py`) with
  `send_message` / `send_file` / `send_video` / `health`, plus `DiscordError`
  mapping Discord error codes (10015 Unknown Webhook, 10003 bad `thread_id`,
  429 rate limit, …) to human-readable reasons.
- Runtime `thread_id` support: `send_message` / `send_file` / `send_video` accept
  an optional `thread_id` to post into an existing Discord thread (query-string
  parameter, per the Discord API). Ignored by Telegram/ntfy (response carries a
  `note`). `thread_id` is a per-call argument, never stored in config.
- `Settings.discord: DiscordConfig | None` — shared display identity
  (`username` default `"mcgram"`, optional `avatar_url`). Credentials never
  live in config: `ChannelConfig.discord_webhook_env` names an env var holding
  the webhook URL, read from `~/.mcgram/.env`.
- `LimitsConfig.discord_file_max_bytes: int = 26214400` — 25 MB effective
  `send_file` / `send_video` cap for Discord channels.
- CLI:
  - `mcgram channel add-discord NAME [--webhook URL] [--env-name E] [-d DESC]` —
    validates the webhook live before writing, prompts securely for the URL when
    `--webhook` is omitted, and never echoes the URL.
  - `mcgram channel list` shows Discord channels as `env=<VAR>` (no URL).
  - `mcgram doctor` checks every Discord channel (webhook liveness + a test send).
  - `mcgram init` offers interactive Discord setup when run in a terminal.
- `env_file.upsert_env_var` — atomic, 0o600, in-place credential writes to
  `~/.mcgram/.env` that preserve existing lines (e.g. `MCGRAM_BOT_TOKEN`).

### Changed
- Config must now declare AT LEAST ONE of `bot`, `ntfy`, or a Discord channel.
  Telegram-only and ntfy-only configs load unchanged.
- `send_message` text length is now capped per transport (Telegram/ntfy 4096,
  Discord 2000). The check runs **after** channel resolution (the cap depends on
  the transport), so a call that is both an unknown channel and over-length now
  reports `unknown_channel` first.
- Tool responses / audit records `transport` field can now be `"discord"`.
  Discord audit records add `discord_webhook_id` (the numeric ID only — never the
  token) and `thread_id`.

### Security
- Discord webhook URLs (address + secret) are stored only in `~/.mcgram/.env`,
  never in `config.yaml`, stdout, or the audit log. Audit records the webhook ID
  segment only. CLI/doctor print channel names and IDs, never the URL.

[0.4.0]: https://github.com/tvtdev94/mcgram/releases/tag/v0.4.0

## [0.3.0] — 2026-07-27

### Fixed
- **Only one Claude Code session could use mcgram at a time.** The second and
  every later session failed to start with `Failed to reconnect to mcgram:
  -32000`. `SingleInstanceLock` wrapped the whole server, so a second process
  hit `LockHeldError` and `sys.exit(1)` before the MCP handshake completed.
  Running several sessions at once is the normal way to use Claude Code, so
  this made mcgram unusable for most real workflows.

  Only Telegram long-polling actually needs to be exclusive (`getUpdates`
  allows one client per bot token; a second gets HTTP 409). Everything else —
  `send_message`, `send_file`, `send_video`, reminders, all of ntfy — is
  one-way HTTP and is safe from any number of processes. The lock now guards
  the poll loop instead of the process.

### Changed
- **Send-only degraded mode.** Instances that don't own the poll loop start
  normally and keep every tool except `ask`. Previously a second session lost
  even ntfy sends, which have nothing to do with Telegram polling.
- **`ask` fails fast when this instance doesn't poll.** It returns immediately
  with `polling_not_owned` and the owning pid, instead of posting the question
  and blocking for the full `ask_timeout_s` — the operator's tap is delivered
  to the *polling* process, so a non-owner could only ever return
  `source: "timeout"`, misreporting an unanswerable question as an ignored one.
  Two adjacent cases get their own reasons rather than being lumped in:
  `telegram_not_configured` (no `bot:` section) and `polling_disabled`
  (`bot.disable_polling`). ntfy channels still return `transport_unsupported`,
  which means something different — that transport has no 2-way input at all.
- **Poll ownership transfers at runtime.** A degraded instance re-checks the
  lock every 30s, so when the owning session exits — cleanly or by `kill -9`,
  which leaves a stale lock — `ask` starts working there without a restart.
  Cadence override: `MCGRAM_POLL_RETRY_S` (seconds).
- `ask` stays in the advertised tool list even where it's degraded: ownership
  can change while the server runs, and MCP clients cache the tool list.
- `mcgram clear-lock` now reports that a running instance will pick up polling
  on its own; the lock no longer gates startup, so a stale one costs `ask`
  rather than the whole server.
- Instances that never poll — ntfy-only, or `bot.disable_polling: true` — no
  longer take the lock at all. Holding it would starve a real Telegram
  instance of `ask` for nothing.

### Security
- Audit log rotation is now safe with several processes writing one file.
  Appends use explicit `O_APPEND`, and rotation/pruning take a short
  cross-process lock (`audit.jsonl.rotate.lock`, auto-reclaimed after 60s if a
  process is killed mid-rotation). Two processes racing the
  `.jsonl → .1 → .2 → .3` renames used to silently drop a backup — a gap the
  single-instance lock had been hiding.
- `MCGRAM_SKIP_LOCK=1` keeps its exact meaning: bypass the lock and accept the
  409 risk of two pollers. The 409 path backs off 10s per conflict.

### Known limitations
- **Reminders are per-process.** `ReminderScheduler` keeps state in memory, so
  each session has its own set: `list_reminders` in session A does not show a
  reminder set in session B, and closing a session drops its reminders. This
  was already true for a single instance ("lost on restart"); with several
  sessions it's more visible.
- The 0.2.0 **PID-recycling collision on Windows** no longer breaks startup.
  A recycled PID now only makes that instance think another one is polling, so
  it runs send-only and loses `ask` — not the `-32000` startup failure. It also
  self-heals: the lock is re-checked every 30s and reclaimed once the recycled
  PID exits. `mcgram clear-lock` still fixes it immediately. Storing process
  creation time alongside the PID remains the proper fix and is still deferred.

### Migration notes
- No config changes. `config.yaml` schema is untouched.
- A second session no longer errors at startup. If you had worked around this
  by setting `MCGRAM_SKIP_LOCK=1`, unset it — the bypass lets both instances
  poll and produces 409 churn. The default now handles multiple sessions.

## [0.2.2] — 2026-07-26

### Added
- `mcgram init` now probes `api.telegram.org` reachability and bakes the result
  into the scaffolded config: `bot.disable_polling` is pre-set (`true` when the
  machine looks Telegram-blocked, `false` when reachable). Skippable via
  `MCGRAM_INIT_NO_TG_PROBE=1` (air-gapped installs / CI).

### Changed
- Default-channel auto-seed (`Settings._seed_default_channel`) now prefers ntfy
  when a bot is configured but `disable_polling=true` (blocked machine) and an
  ntfy topic exists — so notifications land on a reachable transport instead of
  a Telegram channel that can't be polled. Explicit `channels.default` still wins.
- `ask` now rewrites the question message to record the operator's choice
  (`✅ Đã chọn: …` for buttons, `✅ Trả lời: …` for freetext) instead of only
  stripping the inline keyboard — the selection stays visible in chat history.

### Fixed
- `test_version` no longer hard-codes the version string (asserts `__version__`).

## [0.2.1] — 2026-07-26

### Fixed
- README images and doc links now use absolute GitHub URLs so they render on
  the PyPI project page (relative paths only resolve inside the GitHub repo).

## [0.2.0] — 2026-05-22

### Added
- **ntfy.sh transport** — one-way push notifications via the [ntfy.sh](https://ntfy.sh) HTTP API.
  No bot, no token: subscribe to a topic in the ntfy mobile app and receive pings.
  Useful for machines where `api.telegram.org` is blocked.
- `NtfyClient` async HTTP wrapper (`src/mcgram/ntfy_client.py`)
- `NtfyError` exception type
- `dispatch.py` — transport-aware send helpers (`send_text` / `send_document` / `send_video_file`)
- `Settings.ntfy: NtfyConfig | None` — optional `ntfy` config section with
  `server`, `default_topic`, `access_token_env`
- `ChannelConfig.transport: telegram | ntfy` — every named channel declares its transport
- `Settings.resolve_destination(name)` — returns a `Destination` dataclass
  carrying chat_id (telegram) or topic+server (ntfy). `resolve_channel()`
  preserved as legacy alias for telegram-only callers.
- `BotConfig.disable_polling: bool` — keep `bot:` section but skip Telegram
  long-poll on a specific machine (e.g. when api.telegram.org is blocked).
  Eliminates poll-loop log spam in send-only deployments.
- `LimitsConfig.ntfy_file_max_bytes: int = 15728640` — effective `send_file` /
  `send_video` cap for ntfy channels (15 MB matches public free tier).
  Telegram channels keep using `file_max_bytes` (50 MB).
- CLI:
  - `mcgram init` now generates a random ntfy topic (`mcgram-<16-hex>`) and writes
    it to `ntfy.default_topic` in the scaffolded config
  - `mcgram channel add-ntfy NAME [--topic T] [-d DESC]` — declare a ntfy channel
  - `mcgram channel list` shows a transport column
  - `mcgram doctor` runs per-transport checks (telegram + ntfy independent),
    catches `httpx.HTTPError` (SSL/network) and reports as FAIL instead of crashing
  - `mcgram clear-lock` — remove `~/.mcgram/.lock` after a crashed instance.
    Use when MCP fails to start with error -32000 due to a stale PID-recycling
    collision (see Known issues).
- Companion Claude Code skill (`SKILL.md`) rewritten to be transport-aware:
  - Per-tool transport matrix (✅ / ❌ on telegram vs ntfy)
  - Explicit guidance: `ask` is Telegram-only; on ntfy channels, return
    `transport_unsupported` without retry — fall back to `send_message`
  - PII/secrets warning for public ntfy topics
  - Vietnamese trigger phrases added: "gửi qua ntfy", "push to my phone"
- Tests: +57 unit + integration tests (NtfyClient, ntfy config resolution,
  transport branching in tools, ntfy CLI subcommands, doctor per-transport,
  per-transport file size cap). Suite now at 223 tests.

### Changed
- `Settings.bot` is now optional. Config must declare AT LEAST ONE of
  `bot` or `ntfy`; both is valid. Telegram-only configs (pre-0.2 format)
  continue to work unchanged.
- Tool responses now include a `transport` field (`"telegram"` | `"ntfy"`)
  so callers know which backend handled the request.
- Audit log entries gain a `transport` field for the same reason.
- README quickstart now leads with ntfy (faster setup, no token);
  Telegram retained as Option B for `ask` 2-way input.
- `_NEXT_STEPS` printed by `mcgram init` uses ASCII arrows (`->`) instead of
  Unicode `→` for Windows console compatibility on legacy code pages.
- `mcgram init` idempotent path now reads `ntfy.default_topic` from the
  EXISTING config when skipping, so the printed topic always matches what's
  actually on disk.

### Fixed
- `mcgram doctor` previously crashed with an uncaught `httpx.ConnectError`
  when `api.telegram.org` was unreachable. Now reports `[FAIL] telegram ...
  network/SSL error` and continues to check ntfy.

### Security
- ntfy public topics are URL-discoverable. Generated topics use 64-bit
  entropy (16 hex chars). Documentation in README + SKILL.md warns against
  sending PII/secrets unless self-hosting ntfy with auth.
- `ntfy.access_token_env` supports Bearer token auth for paid/self-hosted
  ntfy servers (unit tests cover the path; integration test against a real
  authed server tracked in `TODO(ntfy-auth-e2e)` in `ntfy_client.py`).

### Known issues
- **PID-recycling collision on Windows** — the single-instance lock at
  `~/.mcgram/.lock` stores only the owner PID. If mcgram is killed ungracefully
  (e.g. host reboot) and Windows later assigns the same PID to a different
  long-running process (Python, IDE, etc.), the next `mcgram` invocation will
  see the lock as "held" and refuse to start with error -32000 from Claude
  Code's MCP layer. Workaround: run `mcgram clear-lock`. Proper fix (storing
  process creation time alongside the PID) deferred.

### Migration notes
- 0.1.x configs (`bot:` only) load unchanged on 0.2.0.
- To add ntfy on top of a working 0.1.x setup: either run `mcgram init --force`
  (regenerates config, **erases your operator_chat_id**), or manually append an
  `ntfy:` section to `~/.mcgram/config.yaml` (preserves bot config).
- On machines that block Telegram, set `bot.disable_polling: true` to stop the
  poll loop without removing the bot section.

[0.2.0]: https://github.com/tvtdev94/mcgram/releases/tag/v0.2.0

## [0.1.0] — 2026-05-22

### Added
- MCP stdio server (`mcgram`) exposing 6 tools:
  - `send_message(text, silent?, parse_mode?)` — post text to operator chat
  - `send_file(path, caption?, silent?)` — upload file (≤50 MB, CWD-guarded)
  - `ask(question, options?, timeout_s?)` — block until reply (button/freetext/timeout)
  - `set_reminder(text, delay_s)` — in-process scheduler (≤10 pending, ≤24h)
  - `cancel_reminder(reminder_id)`
  - `list_reminders()`
- Async Telegram Bot API client (`httpx` wrapper, long-poll loop with backoff)
- Operator allowlist enforced at dispatcher entry (rejects non-`operator_chat_id` updates)
- Per-tool token-bucket rate limiter (default 20/min)
- JSONL audit log with `fsync`, 3-backup rotation, optional `redact_text`,
  optional `retention_days` pruning
- Single-instance PID-file lock (cross-platform: Windows + POSIX)
- CLI: `mcgram init`, `mcgram doctor`, `mcgram audit`, `mcgram install-skill`
- Bundled companion Claude Code skill (`SKILL.md`) — installed by `mcgram init`,
  recognizes both English and Vietnamese trigger phrases
- Docs: `README.md`, `docs/architecture.md`, `docs/security-threat-model.md`,
  `docs/manual-smoke-test.md`
- GitHub Actions CI matrix: Python 3.11 + 3.12 × Ubuntu + Windows
- ≥80% test coverage with `pytest` + `pytest-httpx`

### Security
- Bot token loaded from env var, never written to logs / audit / stdout
- `mcgram doctor` masks token to `***<last-4>`
- `send_file` rejects paths outside CWD by default; opt-in `allow_outside_cwd`
- Hard caps on `ask` timeout, reminder count + delay + text length, file size
- Full STRIDE analysis published before v0.1 release

[0.1.0]: https://github.com/tvtdev94/mcgram/releases/tag/v0.1.0
