---
phase: 1
title: Scaffolding & Config & Audit
status: completed
priority: P2
effort: 1d
dependencies: []
---

# Phase 1: Scaffolding & Config & Audit

## Overview

Bootstrap the repository: `pyproject.toml` with `uv` packaging, source layout, config loader (YAML + .env via pydantic-settings), JSONL audit logger with fsync + rotation, and PID-file single-instance lock. Foundation for every later phase.

## Context Links

- Brainstorm report: [`../20260521-brainstorm-mcgram-telegram-mcp/brainstorm-report.md`](../20260521-brainstorm-mcgram-telegram-mcp/brainstorm-report.md)
- Reference repo: `C:\w\dbread` — copy patterns from `pyproject.toml`, `src/dbread/audit.py`, `src/dbread/config.py`

## Requirements

**Functional**
- `pyproject.toml` declares `mcgram` package + CLI entry `mcgram = mcgram.cli:main`
- `mcgram.config.Settings` loads `~/.mcgram/config.yaml` + `.env` with override via `MCGRAM_CONFIG` env var
- `mcgram.audit.AuditLog` writes JSONL: `{ts, tool, status, details}`, fsync per write, rotate at `rotate_mb` (current → .1 → .2 → .3)
- Optional `audit.redact_text: true` replaces `text` field with `"<redacted>"` before write
- Optional `audit.retention_days: N` prunes old entries at startup + hourly
- `mcgram.lock.SingleInstanceLock` acquires `~/.mcgram/.lock` (PID file); raises `LockHeldError` with held PID if active

**Non-functional**
- Every module < 200 LOC
- Python 3.11+ (match dbread)
- All public functions typed; mypy-clean
- Cross-platform (Windows + Linux)

## Architecture

```
src/mcgram/
├── __init__.py          # version export
├── config.py            # pydantic Settings, YAML + .env loader
├── audit.py             # JSONL writer + rotation + retention
├── lock.py              # PID file lock (cross-platform via os.O_EXCL)
└── errors.py            # MCGramError, LockHeldError, ConfigError, RateLimitError, AuthError

pyproject.toml           # uv-managed
config.example.yaml      # template copied by `mcgram init`
.env.example
.gitignore               # exclude .env, audit.jsonl, .lock, dist/, .venv/
LICENSE                  # MIT
```

### Config schema (pydantic)

```python
class BotConfig(BaseModel):
    token_env: str = "MCGRAM_BOT_TOKEN"
    operator_chat_id: int

class DefaultsConfig(BaseModel):
    parse_mode: Literal["plain", "markdown_v2"] = "plain"
    ask_timeout_s: int = Field(120, ge=1, le=600)
    rate_limit_per_min: int = Field(20, ge=1, le=600)

class LimitsConfig(BaseModel):
    ask_timeout_max_s: int = 600
    reminder_max_delay_s: int = 86400
    reminder_max_pending: int = 10
    file_max_bytes: int = 52_428_800
    ask_options_max: int = 6

class AuditConfig(BaseModel):
    path: Path = Path("~/.mcgram/audit.jsonl")
    rotate_mb: int = 25
    redact_text: bool = False
    retention_days: int | None = None
    timezone: str = "UTC"

class Settings(BaseSettings):
    bot: BotConfig
    defaults: DefaultsConfig = DefaultsConfig()
    limits: LimitsConfig = LimitsConfig()
    audit: AuditConfig = AuditConfig()
```

### Audit record shape

```jsonc
{"ts":"2026-05-21T10:00:00+00:00","tool":"send_message","status":"ok","chat_id":123,"text_len":42,"ms":150}
{"ts":"2026-05-21T10:00:05+00:00","tool":"ask","status":"timeout","question_id":"q_abc","timeout_s":120}
{"ts":"2026-05-21T10:00:10+00:00","tool":"send_file","status":"rejected","reason":"file_too_large","bytes":60000000}
```

## Related Code Files

- **Create:**
  - `pyproject.toml`
  - `src/mcgram/__init__.py`
  - `src/mcgram/config.py`
  - `src/mcgram/audit.py`
  - `src/mcgram/lock.py`
  - `src/mcgram/errors.py`
  - `config.example.yaml`
  - `.env.example`
  - `.gitignore`
  - `LICENSE`
  - `README.md` (stub — full content in Phase 5)

## Implementation Steps

1. **pyproject.toml** — copy dbread layout: name=`mcgram`, deps `mcp>=1.0`, `httpx>=0.27`, `pydantic>=2.5`, `pydantic-settings`, `pyyaml`. Extras `[dev]`: `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `mypy`. Console script `mcgram = mcgram.cli:main`.
2. **errors.py** — minimal exception hierarchy.
3. **config.py** — pydantic Settings; `Settings.load()` reads `MCGRAM_CONFIG` env (default `~/.mcgram/config.yaml`), then loads sibling `.env`. Expand `~`. Resolve `token_env` → fail loudly if env var missing.
4. **audit.py** — port dbread/src/dbread/audit.py: `AuditLog.write(record: dict)`, fsync each write, rotate at `rotate_mb`, optional `redact_text` (replace `text` with `<redacted>`, keep `text_len`), optional `retention_days` prune (startup + once-per-hour).
5. **lock.py** — `acquire(path: Path) -> int`: `os.open(path, O_CREAT|O_EXCL|O_WRONLY)`, write PID. On EEXIST, read PID, check alive (`os.kill(pid, 0)` on POSIX, `OpenProcess` on Windows), raise `LockHeldError(pid)`. Stale lock auto-cleanup.
6. **Templates** — `config.example.yaml` with full schema commented; `.env.example` with `MCGRAM_BOT_TOKEN=`.
7. **Verify** — `uv sync`, `python -c "from mcgram.config import Settings"`, manual lock test (two processes).

## Success Criteria

- [ ] `uv sync --extra dev` succeeds clean
- [ ] `mcgram` console script registered (verify `which mcgram` or `python -m mcgram --help` stub)
- [ ] `Settings.load()` with sample config + `.env` returns typed object; bad config raises pydantic `ValidationError`
- [ ] `AuditLog.write()` produces JSONL line, fsync confirmed, rotates at threshold, redact mode strips `text`
- [ ] `SingleInstanceLock` blocks second acquire and releases on context exit
- [ ] `ruff check src/` clean
- [ ] All files < 200 LOC

## Risk Assessment

- **Windows PID-alive check**: `os.kill(pid, 0)` raises on Windows. Need `ctypes` `OpenProcess` or `psutil` fallback. → use stdlib only via `ctypes`.
- **Pydantic v2 BaseSettings nesting**: env_nested_delimiter needs explicit. → set `env_nested_delimiter="__"` and document.
- **Rotation race on Windows**: `os.rename` over existing file fails on Windows. → delete-then-rename pattern.

## Security Considerations

- Bot token NEVER written to audit log
- Default audit perms 0600 (best-effort on Windows)
- `.env` always gitignored

## Next Steps

Phase 2 — Telegram client + send tools.
