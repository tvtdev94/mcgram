# Changelog

All notable changes to mcgram will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/).

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
