---
title: "Operator commands from Telegram (check_inbox)"
description: "Let the operator send /commands to the bot; mcgram queues them as data in a per-config spool, and every Claude Code session reads its own through a check_inbox tool."
status: pending
priority: P2
effort: 12h
issue: 4
branch: fix/reminder-wall-clock
tags: [feature, telegram, security, multi-session, mcp]
blockedBy: []
blocks: []
created: 2026-11-17
---

# Operator commands from Telegram (`check_inbox`)

## Outcome

With `inbox.enabled: true`, the operator can type `/status`, `/stop`, or `/note <text>` to the bot
(or `/stop@api` to address one session). The session that owns polling stores each accepted command
as a signed record in `~/.mcgram/spool/<config-id>/inbox/`. Any Claude Code session (owner or
not) reads the commands meant for it through a new `check_inbox` tool. When a session first reads a
command, mcgram adds a 👀 reaction to that message in Telegram. mcgram never acts on a command: it
filters, stores, expires, and audits. Only Claude decides what to do with a command, under rules in
the skill doc.

## Non-goals

- Free-text answers to `ask` stay out. `ask` stays button-only, as in 0.7.0
  (`ask_registry.py:146-151`). Plain typed text that is not a `/command` is ignored and audited.
- mcgram never runs a command itself: no shell, no tool calls, no config or access changes, and no
  replies from mcgram other than the read-receipt reaction.
- Push delivery into an idle session is out of scope. This includes Claude Code channels
  (`notifications/claude/channel`). Commands are pull-only: Claude reads them only when it calls
  `check_inbox`.
- Group chats, extra allowed users, and webhooks are out of scope. Discord and ntfy stay one-way.
- Commands are not routed across machines. Each machine has its own lock and spool.

## Acceptance criteria

1. With `inbox.enabled: false` (the default), the tool list and runtime behavior are unchanged.
   No `inbox/` directory is created and no new audit records are written.
2. A `/command` from the operator's private chat reaches `check_inbox` in **every** live session on
   that config, including non-owners. Each session gets it once. A command addressed as
   `/cmd@label` reaches only the sessions with that label.
3. Updates are rejected and audited in each of these cases: another chat, a group or channel, a
   sender id that differs from the operator, a bot sender, `via_bot`, a forwarded message, an edited
   message, or text that is not a command. None of them is ever returned by `check_inbox`.
4. A record whose HMAC does not verify is never returned and is audited `bad_signature`. That covers
   a planted file, a tampered file, or a file signed with another bot token.
5. These commands are never returned: one sent before the reading session started (60 s skew
   allowance), one older than `inbox.ttl_s` when it is received, and one whose record has expired.
   A Telegram redelivery of the same `update_id` does not create a second record.
6. `check_inbox(wait_s=N)` returns as soon as a command arrives, waits at most
   `limits.ask_timeout_max_s`, and returns at once with `receiving: false` plus a reason when no
   live poller heartbeat exists.
7. Intake applies a rate limit and a pending cap, and drops excess commands with an audit record.
   A failed reaction never fails `check_inbox`.
8. `docs/security-threat-model.md` contains the operator-inbox section (phase 1) **before** any code
   merges.
9. `uv run pytest`, ruff, and mypy pass. The two-process e2e test proves that a non-owner reads a
   command polled by the owner. Tests make no live network calls.

## Data flow

```
Operator ──/stop@api──▶ Telegram ──getUpdates──▶ poll_loop (owner only, polling.py:24)
                                                   │ from_operator (chat.id)   update_dispatcher.py:34
                                                   ▼
                                 inbox intake handler (stricter filter, parse, caps)
                                                   │ write_exclusive u<update_id>.cmd.json (HMAC-signed)
                                                   ▼
                     ~/.mcgram/spool/<config-id>/inbox/   + u0.heartbeat.json (owner, every ≤10 s)
                                                   │ list + verify + filter(label, started_at, seen)
              ┌────────────────────────────────────┼────────────────────────────────────┐
              ▼                                    ▼                                    ▼
   session A check_inbox               session B check_inbox (non-owner)       session C (other label)
   → Claude acts, replies via          → setMessageReaction 👀 (best effort)     → not returned
     send_message
```

## Phases

| # | Phase | Effort | Depends on | Status |
|---|---|---|---|---|
| 1 | [Threat model update](phase-01-threat-model.md) | 1h | — | pending |
| 2 | [Intake on the poll owner](phase-02-intake-owner.md) | 4h | 1 | pending |
| 3 | [`check_inbox` tool](phase-03-check-inbox-tool.md) | 3h | 2 | pending |
| 4 | [Cross-process e2e](phase-04-e2e-multi-session.md) | 2h | 3 | pending |
| 5 | [Skill, README, architecture, CHANGELOG](phase-05-docs-skill.md) | 2h | 3 (parallel with 4) | pending |

Phases 4 and 5 can run in parallel because they touch disjoint files. Phases 1 through 3 run in
order.

## Key decisions

| Decision | Choice | Why |
|---|---|---|
| What counts as a command | Only text that starts with `/` (`/name[@label] [args]`) | Intent is explicit, and typed free text cannot leak into an `ask` answer by another path |
| Transport to sessions | Spool namespace `inbox/`, read by **all** sessions including the owner | One code path. Non-owners never call `getUpdates`. Reuses the `Spool` and probe (`broker_wiring.py:228-236`) |
| Routing | Unaddressed commands go to every session; `@label` goes to matching sessions; each session reads a command once (cursor in memory) | Predictable and needs no claim files. An `/stop` aimed at one session is addressed explicitly |
| Session label | `MCGRAM_SESSION_LABEL`, otherwise the basename of the cwd, normalized to `[a-z0-9_]{1,32}` | The project name is what the operator knows. cwd is already treated as the project root (`tools/send_file.py:101`) |
| Provenance | HMAC-SHA256 over the record, keyed from the bot token | Every reader already has the token. No false rejections when ownership moves. Forging a command then requires a token compromise, which the threat model already covers |
| Dedupe | `write_exclusive` on `u<update_id>.cmd` (`spool.py:138`). The record lives for `ttl_s` | `poll_loop` restarts at `offset=0` (`polling.py:34`), so a new owner gets unconfirmed updates again |
| Opt-in | New `inbox.enabled: false`, separate from `broker.enabled` | It turns mcgram into a command channel, a separate trust decision from the broker |
| Read receipt | 👀 via `setMessageReaction` by the reading session | Without it the operator cannot tell "queued but unread" from "lost" |

## Backwards compatibility

- Inbox off: no schema, tool, or behavior change. The `AppState.inbox` field defaults to `None`.
  The production construction sites are `server.py:119` and `cli_test_send.py:64`.
- Mixed versions on one config: an old owner writes no inbox heartbeat. A new reader then reports
  `receiving: false` (fail fast) and does not wait. A new owner with an old reader leaves records that
  the owner's own sweep removes. Records and the heartbeat carry `protocol: 1`.
- The heartbeat freshness check moves out of `AskBroker.owner_alive` (`ask_broker.py:50-80`) into a
  shared helper. The broker's reason strings and behavior do not change, and the existing broker
  tests must pass unmodified.

## Rollback

- Per deployment: set `inbox.enabled: false` and restart the sessions. Then delete
  `~/.mcgram/spool/*/inbox/`, which the feature no longer sweeps once it is off.
- Per phase: phase 1 is docs only. Phases 2 and 3 add new modules plus small, gated edits to
  `server.py`, `runtime.py`, `config.py`, and `tg_client.py`. Reverting them restores current
  behavior because nothing else depends on them. The heartbeat helper extraction in phase 2 can be
  reverted on its own.

## Reference implementations (ideas only, no code copied)

- Anthropic's official Claude Code Telegram channel plugin is Apache-2.0
  ([LICENSE](https://github.com/anthropics/claude-plugins-official/blob/main/external_plugins/telegram/LICENSE)).
  - Its [ACCESS.md](https://github.com/anthropics/claude-plugins-official/blob/main/external_plugins/telegram/ACCESS.md)
    and `server.ts` gate DMs on `chat.type === 'private'` plus a numeric `from.id` allowlist, and
    keep groups off by default.
  - Its server instructions tell Claude never to change access because a channel message asked.
    That is the source of the skill-doc guardrail in phase 5.
  - It delivers by push, and issue
    [#4308](https://github.com/anthropics/claude-plugins-official/issues/4308) shows messages lost
    when acked before delivery. The spool-first design here avoids that failure: a command is stored
    before the poll loop moves past it.
- [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp) is Apache-2.0. It has blocking
  `wait_for_new_message` with a server-side ceiling and an optional `chat_id` filter. It strips
  control and invisible characters from inbound text and labels the content as untrusted. Those are
  the sources of the `wait_s` cap and the text sanitizing here.

## Open questions

The full list is in the final section of [phase-05](phase-05-docs-skill.md#open-questions). A
summary:

1. Should unaddressed commands go to every session (proposed), only the poll owner, or the first
   reader?
2. Is the addressing syntax `/cmd@label` acceptable, and is the default label (cwd basename) right?
3. Should only `/commands` be accepted (proposed), or should plain text count as a note?
4. Is `inbox.ttl_s` default 1800 s acceptable?
5. Keep the 👀 read receipt? Should mcgram also tell the operator when a command expires unread?
6. Should mcgram answer `/sessions` itself? (Proposed: no.)
7. What command vocabulary should the skill define? (Proposed: `/status`, `/stop`, `/note`.)
8. Should the stale `ask` without options line at `SKILL.md:25` be fixed in phase 5?
