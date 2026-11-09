---
title: "mcgram 0.6.0"
status: in-progress
branch: feat/v0.6.0
issue: https://github.com/tvtdev94/mcgram/issues/2
created: 2026-11-09
---

# mcgram 0.6.0

Objective: release 0.6.0 with items 1–8 below plus cleanup, then publish per
`docs/release.md`. Deferred: #3 (ask over ntfy), #4 (Telegram remote control).

## Constraints

- MCP contract is additive only: existing tool names, arguments and result keys keep working.
- Broker stays opt-in (`broker.enabled: false` default).
- CI matrix py3.11/3.12 × ubuntu/windows stays green.

## Streams

| Stream | Items | Owns | Status |
|---|---|---|---|
| A — ask & reminders | 1 broker phases 2–5 (`../260729-2106-ask-broker-multi-session/`), 7 durable reminders, 8 richer `ask` | `ask_*`, `spool*`, `reminders.py`, `tools/ask.py`, `tools/*reminder*.py`, `runtime.py`, broker config, matching tests, broker docs | Pending |
| B — send path | 2 `edit_message`, 3 ntfy priority/title/tags, 4 long text as `.txt`, 5 Telegram 429 retry, 6 inline images, `mcgram test-send` | `tg_client.py`, `ntfy_client.py`, `discord_client.py`, `dispatch.py`, `tools/send_*.py`, `tools/edit_message.py`, `cli.py`, `cli_test_send.py`, matching tests | Pending |
| Integrate | merge A+B, `server.py` tool list, README/CHANGELOG/docs, version bump, isolated review, live smoke, publish | orchestrator | Pending |

Streams A and B run in parallel; `server.py` tool registration is the only shared
file and is merged by the orchestrator.

## Decisions

- Long text: deliver as a `.txt` attachment whose caption is the start of the text, instead of splitting into several messages (one notification, full log kept intact).
- `edit_message` on ntfy returns `transport_unsupported`.
- `ask_registry.py` stays untouched during broker phases (broker acceptance criterion); item 8 changes it afterwards.

## Acceptance

- Every item above implemented with tests; `uv run pytest -q` and `uv run ruff check src/ tests/` green; CI green on the release commit.
- Live smoke on Telegram `default`, Discord `smoke-test`, a fresh ntfy.sh topic, including an `ask` answered by the operator.
- 0.6.0 on PyPI, tag `v0.6.0`, GitHub release; CHANGELOG, README, docs updated.
