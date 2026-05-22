---
phase: 5
title: Tests & Docs & CI
status: completed
priority: P2
effort: 2d
dependencies:
  - 1
  - 2
  - 3
  - 4
---

# Phase 5: Tests & Docs & CI

## Overview

Lock in correctness with pytest (≥80% coverage), ship public-facing docs (README, threat model, architecture), and wire GitHub Actions CI matrix. Final phase that turns the working code into a publishable PyPI product matching `dbread` quality bar.

## Context Links

- Brainstorm: [`../20260521-brainstorm-mcgram-telegram-mcp/brainstorm-report.md`](../20260521-brainstorm-mcgram-telegram-mcp/brainstorm-report.md) §6 (success criteria)
- Reference: `C:\w\dbread\tests\`, `C:\w\dbread\.github\workflows\ci.yml`, `C:\w\dbread\README.md`, `C:\w\dbread\docs\security-threat-model.md`

## Requirements

**Functional**
- Unit tests cover: config loading, audit writer + rotation + redact + retention, lock acquire/release, rate limiter, tg_client (httpx mocked), ask_registry (timeout + button + freetext + concurrent), reminders (create + cancel + list + delay precision), tools (each with happy path + reject path)
- Subprocess smoke test: spawn `mcgram` via stdio, drive `tools/list` + `tools/call send_message`, assert success (with mocked Telegram endpoint via local httpx mock server)
- Coverage report ≥ 80% overall
- `ruff check src/ tests/` clean
- `mypy --strict src/` passes for public modules

**Non-functional**
- CI matrix: Python 3.11, 3.12 × Ubuntu, Windows (4 jobs)
- CI runs: lint, type-check, unit tests, coverage
- README quickstart works copy-paste (verified manually)
- PyPI metadata: classifiers, description, license, project URLs

## Architecture

```
tests/
├── conftest.py                 # fixtures: tmp config, fake httpx server, fake audit
├── test_config.py
├── test_audit.py               # write, fsync, rotate, redact, retention
├── test_lock.py                # acquire, release, stale, contention
├── test_rate_limiter.py
├── test_tg_client.py           # all API methods with mocked httpx
├── test_ask_registry.py        # button, freetext, timeout, concurrent, race
├── test_reminders.py           # fire, cancel, list, limits
├── test_tools_send_message.py
├── test_tools_send_file.py     # size cap, path traversal
├── test_tools_ask.py
├── test_tools_reminder.py
├── test_cli_init.py
├── test_cli_doctor.py
├── test_cli_audit.py
├── test_skill_installer.py
└── smoke/
    └── test_stdio_smoke.py     # subprocess + fake TG server

docs/
├── architecture.md             # module diagram + data flows
├── security-threat-model.md    # STRIDE analysis
├── manual-smoke-test.md        # checklist for verifying integration with Claude Code
└── images/                     # mermaid renders (optional)

.github/workflows/
└── ci.yml                      # matrix: py 3.11/3.12 × ubuntu/windows

README.md                       # hero + why + quickstart + tools + security + audit + config + limits
CHANGELOG.md                    # follow Keep-a-Changelog
```

### CI workflow shape

```yaml
name: ci
on: [push, pull_request]
jobs:
  test:
    strategy:
      matrix:
        python: ["3.11", "3.12"]
        os: [ubuntu-latest, windows-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv python install ${{ matrix.python }}
      - run: uv sync --extra dev
      - run: uv run ruff check src/ tests/
      - run: uv run mypy --strict src/mcgram
      - run: uv run pytest --cov=mcgram --cov-report=xml
      - uses: codecov/codecov-action@v4
        if: matrix.python == '3.12' && matrix.os == 'ubuntu-latest'
```

### Fake Telegram server (for tests + smoke)

Lightweight `httpx.MockTransport` or `pytest-httpx` plugin: matches `https://api.telegram.org/bot{token}/(sendMessage|sendDocument|getUpdates|answerCallbackQuery|editMessageReplyMarkup|getMe)`, returns canned JSON. Supports injecting fake incoming updates into the next `getUpdates` response (drives ask_registry tests).

## Related Code Files

- **Create:**
  - `tests/conftest.py` + 16 test modules above
  - `tests/smoke/test_stdio_smoke.py`
  - `tests/fake_tg.py` — mock transport helper
  - `docs/architecture.md`
  - `docs/security-threat-model.md`
  - `docs/manual-smoke-test.md`
  - `.github/workflows/ci.yml`
  - `README.md` (full content, replacing Phase 1 stub)
  - `CHANGELOG.md` (initial v0.1.0 entry)
- **Modify:**
  - `pyproject.toml` — add `[tool.pytest.ini_options]`, `[tool.ruff]`, `[tool.mypy]`, `[tool.coverage]` sections + `pytest-httpx` dev dep

## Implementation Steps

1. **conftest.py + fake_tg.py** — central fixtures: `tmp_config`, `fake_client` (httpx with MockTransport), `audit_tmp` (writes to tmp_path).
2. **Unit tests** — write per module, target ≥80% line coverage. Critical edge cases:
   - Audit: fsync called, rotation chain `current→.1→.2→.3`, redact strips `text` keeps `text_len`, retention prunes only matching lines, malformed lines kept (fail-safe).
   - Lock: stale PID auto-cleanup, live PID blocks, Windows + POSIX both pass.
   - Ask: concurrent asks resolved independently, freetext after button → button wins, button after timeout → ignored gracefully.
   - Reminders: ±200ms precision for 1s delays, cancel before fire prevents send, shutdown cancels all.
   - send_file: 50 MB+1 byte rejected, `..\..\file` rejected.
3. **Smoke test** — `subprocess.Popen("mcgram", stdin=PIPE, stdout=PIPE, env={MCGRAM_CONFIG: tmp_yaml, TELEGRAM_API_ROOT: localhost:PORT})`. Note: tg_client must respect optional `api_root_env` override so tests can redirect to mock server.
4. **docs/architecture.md** — module diagram + data flow (mermaid optional).
5. **docs/security-threat-model.md** — STRIDE analysis of the 5 layers:
   - S(poofing): non-operator updates → Layer 1 rejects
   - T(ampering): bot token theft → 0.6 (env file), audit redact
   - R(epudiation): audit log + fsync
   - I(nformation disclosure): never log token, redact_text option
   - D(enial of service): rate limit + ask timeout cap + reminder cap
   - E(levation of privilege): personal bot, no group adds documented
6. **README.md** — full quickstart matching dbread style: hero/why/quickstart 2-min/tools table/security model table/audit/config example/known limitations/update/credits.
7. **CHANGELOG.md** — `0.1.0 (2026-05-21)` initial release notes.
8. **CI workflow** — matrix as above. Add badge to README.
9. **PyPI prep**:
   - `pyproject.toml` metadata: description, keywords, classifiers, project.urls
   - `uv build` produces wheel + sdist
   - Manual `uv publish --token ...` (not in CI)

## Success Criteria

- [ ] `uv run pytest` → all tests pass on both Python 3.11 + 3.12
- [ ] Coverage ≥ 80% overall, ≥ 90% for guards (rate_limiter, audit, lock)
- [ ] `ruff check src/ tests/` clean
- [ ] `mypy --strict src/mcgram` clean
- [ ] CI matrix green on Ubuntu + Windows
- [ ] Smoke test drives full stdio flow with fake TG and exits clean
- [ ] README quickstart copy-pasted by a fresh user installs + runs in <2 minutes (manual verify)
- [ ] `uv build` produces wheel; `pipx install dist/*.whl` works
- [ ] `mcgram doctor` after install passes against real test bot
- [ ] PyPI metadata renders correctly on test.pypi.org

## Risk Assessment

- **httpx MockTransport vs polling loop**: polling uses long-lived `getUpdates` with `timeout=25` — MockTransport must respond fast in tests (override `timeout=0` via test fixture).
- **Windows file rotation flakiness**: rotation uses `os.rename` over existing file → delete-first then rename (covered in Phase 1 risk). Add Windows-specific rotation test.
- **subprocess smoke test stdin/stdout**: MCP SDK stdio mode expects newline-delimited JSON-RPC; use `mcp` SDK's test client if available, else hand-craft messages.
- **Codecov flakiness**: only upload from one matrix cell; tolerate codecov action failure.

## Security Considerations

- Threat model published before v0.1 release (matches dbread principle: "honesty about known limits")
- README `Known Limitations` section lists in-process scheduler, single-instance assumption, no Atlas-like advanced features
- License MIT (match dbread)

## Next Steps

After this phase: `0.1.0` ready for PyPI publish. Future v0.2 ideas (deferred):
- `read_inbox` for polling-based commands
- Persistent reminders (SQLite)
- Multi-channel routing
- Telegram /commands integration (bot menu)
