# Changelog

All notable changes to mcgram will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/).

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
