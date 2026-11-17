---
title: "Operator commands from Telegram (check_inbox)"
description: "Let the operator send /commands to the bot; mcgram queues them as data in a per-config spool, and every Claude Code session reads its own through a check_inbox tool."
status: completed
priority: P2
effort: 12h
issue: 4
branch: feat/telegram-inbox
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
| 1 | [Threat model update](phase-01-threat-model.md) | 1h | — | completed |
| 2 | [Intake on the poll owner](phase-02-intake-owner.md) | 4h | 1 | completed |
| 3 | [`check_inbox` tool](phase-03-check-inbox-tool.md) | 3h | 2 | completed |
| 4 | [Cross-process e2e](phase-04-e2e-multi-session.md) | 2h | 3 | completed |
| 5 | [Skill, README, architecture, CHANGELOG](phase-05-docs-skill.md) | 2h | 3 (parallel with 4) | completed |

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

## Decisions (user-approved, 2026-11-17)

The user approved the plan, including the phase 1 threat-model section as proposed, and answered
the open questions as follows:

1. **Routing.** An unaddressed command goes to every live session, so a bare `/stop` reaches all of
   them.
2. **Addressing.** `/cmd@label`. The default label is the working folder name; `MCGRAM_SESSION_LABEL`
   overrides it.
3. **What counts.** Only `/commands`. Plain text stays ignored (no implicit `/note`).
4. **Expiry.** `inbox.ttl_s` defaults to 1800 s (allowed 60–86400 s), with a 60 s clock allowance
   for "sent before this session started".
5. **Read receipt.** Keep the 👀 reaction (best effort). mcgram posts **no** message when a command
   expires unread.
6. **`/sessions`.** mcgram does not answer it (non-goal).
7. **Vocabulary.** `/status`, `/stop`, `/note` only. No `setMyCommands` registration.
8. **`SKILL.md` "ask without options" row.** Fixed in a separate PR (#13); this plan does not touch
   that line.

## Implementation notes and deviations

Implemented on `feat/telegram-inbox`. The line references above were written against another
branch; the code follows the actual modules on `origin/main`. Small deviations, all stricter or
equivalent:

- `parse_update` also rejects (`rejected/malformed`) an update whose `update_id`, `message_id` or
  `date` is not an integer, so a record can never carry an unusable id or time.
- `publish` tells a failed write (`dropped/write_failed`, audited and logged) apart from a
  duplicate, and a redelivery that arrives while the inbox is full is still a silent duplicate,
  not `inbox_full`.
- `unread` also rejects a genuine record stored under another update id than the one it was signed
  for (audited `bad_signature`), so renaming a record cannot replay a command.
- `spool.heartbeat_status` takes the heartbeat dict; each caller words the missing-heartbeat case
  (broker unchanged; inbox: "no session is polling Telegram for commands").
- `check_inbox` rejects any `wait_s` that is not a non-negative integer (bool, float, string) as
  `invalid_input`. Read receipts run concurrently, each under the 5 s timeout, so a batch of 20
  never blocks for 100 s.
- The supervisor ticks every `broker.poll_interval_s`, so a demoted owner withdraws its heartbeat
  within one tick.
- After code review: the per-session read cursor maps each key to its record's expiry and is pruned
  by expiry only (the plan pruned it to the keys on disk, which a failed listing turns into a
  replay); a command is marked read after its audit record is written; `expires_at` is
  `max(received_at, sent_at) + ttl_s`, so a clock behind Telegram's cannot reopen dedupe; a deleted
  working folder falls back to the `session` label. Kept as planned: the intake rate token is
  taken before the duplicate/stale checks, and the heartbeat follows lock ownership rather than
  `getUpdates` health (documented in `docs/architecture.md`).
- After the PR review (controller decisions):
  - Decision 7 is enforced at intake: only `/status`, `/stop`, `/note` are stored; anything else is
    `dropped/unknown_command`. `/stop @api` (target after a space) is `dropped/ambiguous_target`.
  - Muting resistance: only signed, unexpired records count toward the 50-record cap; a file
    already under an update's name is verified and replaced unless genuine
    (`queued` + `replaced: bad_signature`); redeliveries are audited `ignored/duplicate`; records
    claiming an expiry beyond `86400 + 60` s are swept.
  - Every `dropped` intake outcome gets a best-effort 👎 reaction from a background task (own
    bucket). ⚠️ was the first choice, but it is not on Telegram's ReactionTypeEmoji list. This does
    not change decision 5: nothing is posted when a command expires unread.
  - Lone surrogates are stripped from args; with the inbox off, an existing `inbox/` directory is
    swept once at startup.
  - Docs: NTP-synced clock requirement, label clashes, `/stop@BotName` and hyphenated labels.
- Tests add a few cases beyond the plan's tables: the disabled inbox leaves no trace, a non-owner
  never removes another pid's heartbeat, label normalization, the `wait_s` cap, the 20-command batch
  with `more`, and two more forgery cases (another bot's key, a renamed record).
