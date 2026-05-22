---
title: Implement mcgram - Telegram MCP for Claude Code
description: >-
  Python MCP server bridging Claude Code to a personal Telegram bot. 6 tools:
  send_message, send_file, ask (inline buttons + timeout), set_reminder,
  cancel_reminder, list_reminders. Style mirrors dbread.
status: completed
priority: P2
branch: ''
tags:
  - mcp
  - telegram
  - python
  - uv
blockedBy: []
blocks: []
created: '2026-05-21T15:22:18.540Z'
createdBy: 'ck:plan'
source: skill
---

# Implement mcgram - Telegram MCP for Claude Code

## Overview

Build a Python MCP server (`mcgram`) that lets Claude Code report progress, ask short Q&A (approve/reject/pick), send files, and set in-session reminders via a personal Telegram bot. Single process — bot polling lives inside MCP server. Style + packaging mirror `C:\w\dbread` exactly (uv tool install, `~/.mcgram/config.yaml`, JSONL audit, CLI subcommands, companion Claude skill, PyPI + GitHub + CI).

Full design rationale: [`../20260521-brainstorm-mcgram-telegram-mcp/brainstorm-report.md`](../20260521-brainstorm-mcgram-telegram-mcp/brainstorm-report.md).

## Phases

| Phase | Name | Status |
|-------|------|--------|
| 1 | [Scaffolding & Config & Audit](./phase-01-scaffolding-config-audit.md) | Completed |
| 2 | [Telegram Client & Send Tools](./phase-02-telegram-client-send-tools.md) | Completed |
| 3 | [Ask Flow & Reminders](./phase-03-ask-flow-reminders.md) | Completed |
| 4 | [CLI & Companion Skill](./phase-04-cli-companion-skill.md) | Completed |
| 5 | [Tests & Docs & CI](./phase-05-tests-docs-ci.md) | Completed |

## Phase Dependencies

```
1 (foundation) ──▶ 2 (client+send) ──▶ 3 (ask+reminders) ──▶ 4 (cli+skill) ──▶ 5 (tests+docs+ci)
```

Each phase blocks the next. Phase 5 covers tests/docs/CI for everything in 1–4.

## Key Decisions (from brainstorm)

- **Scope:** Report + short Q&A (inline buttons + timeout). No remote-control. No daemon.
- **Lifetime:** Bot polls only while MCP process alive. Reminders lost on shutdown.
- **Channel:** Single `operator_chat_id` (personal bot DM).
- **Stack:** `httpx` (async) + `mcp` SDK + `pydantic` v2 + `pyyaml`. No `python-telegram-bot` / `aiogram`.
- **Tools:** 6 — `send_message`, `send_file`, `ask`, `set_reminder`, `cancel_reminder`, `list_reminders`.
- **Distribution:** PyPI + GitHub public + companion Claude skill. English docs.
- **Constraints:** Modules <200 LOC. ≥80% test coverage. CI matrix 3.11/3.12 × Ubuntu/Windows.

## Out of Scope

- Always-on daemon / cross-restart reminders
- Webhook deployment / VPS
- Claude Code hook auto-notify
- Multi-channel routing, group topics
- `read_inbox`, `edit_message`, `delete_message`
- i18n docs

## Dependencies

No cross-plan dependencies — brand-new project. Reference repo: `C:\w\dbread` for every style decision.
