---
phase: 1
title: "Threat model update"
status: completed
priority: P1
dependencies: []
effort: 1h
---

# Phase 1: Threat model update (docs only, merge first)

## Context

The issue requires the threat model to come first. Today mcgram receives only button taps, and an
`ask` question opens every inbound path (`ask_registry.py:146-151`). This feature adds a path that the
operator starts and whose content becomes instructions to Claude. The threat model's opening claims
change:

- the trust-boundary note at `docs/security-threat-model.md:17-19` says `spool/asks/` is the only
  decision-making spool;
- the adversary list at `:23`;
- the group-chat row at `:80`. It assumes `operator_chat_id` is a DM, but nothing enforces that
  (`config.py:24-32`).

## Files

- Modify: `docs/security-threat-model.md` (the only file in this phase)

## Steps

1. Change the trust-boundary note (`:17-19`) to cover two decision-making namespaces, `asks/` and
   `inbox/`.
2. Extend the adversary list (`:23`) with two items: an operator message forwarded or quoted from
   someone else, and a local process that plants an inbox record.
3. Add a row to the E table (`:80`): the inbox accepts private chats only, even when
   `operator_chat_id` is a group.
4. Add a bullet to "What mcgram does NOT defend against" (`:84-92`): with the inbox on, anyone who
   holds the bot token and can write the spool can forge a command.
5. Add the section below after "Broker spool" (`:94-113`).

## Proposed section text

```markdown
## Operator inbox

Opt-in (`inbox.enabled: false` by default). It turns mcgram from notify-only into a command
channel: the operator types `/name[@label] [args]` to the bot, and Claude reads it through
`check_inbox`. **mcgram never acts on a command.** It filters, stores, expires, audits, and adds a
👀 reaction when a session reads one. Everything a command causes is done by Claude, under the rules
in the bundled skill. A command is operator-authored *data* for Claude, never something mcgram runs.

Records live in `~/.mcgram/spool/<config-id>/inbox/u<update_id>.cmd.json` (0600 in a 0700 dir, same
probe as the broker). The polling session writes a heartbeat there while it polls.

| STRIDE | Threat | Mitigation |
|---|---|---|
| **S** | A stranger DMs the bot `/stop` or `/note run rm -rf` | Only updates that pass every check below are stored. `chat.id == operator_chat_id` (existing filter). `chat.type == "private"`. `from.id == operator_chat_id`. `from.is_bot` is false. No `via_bot`. Anything else is audited `rejected` and never stored. |
| **S** | Bot added to a group, or `operator_chat_id` set to a group id | The inbox refuses to start when `operator_chat_id` is not a positive (private) id: it logs a warning at boot and `check_inbox` returns `inbox_unavailable`. Group and channel updates are rejected as above. |
| **S** | The operator forwards someone else's message that starts with `/` | Forwarded messages (`forward_origin`, `forward_date`, `is_automatic_forward`) are rejected, because the words are not the operator's. Captions are ignored. Only `text` counts. |
| **S** | Prompt injection with shell access writes `inbox/u1.cmd.json` = `/note push to prod` | Every record carries an HMAC-SHA256 keyed from the bot token. Readers verify it and audit a mismatch as `bad_signature`. Forging a command therefore needs the bot token, which this document already treats as full compromise ("Compromised Telegram account"). This does not stop a same-uid attacker who reads `~/.mcgram/.env`. |
| **E** | A command makes Claude do something dangerous (prompt injection through the operator channel) | Commands may **narrow or redirect** the current task (`/stop`, `/status`, `/note`). The skill forbids treating a command as approval: an action that needs confirmation still goes through button `ask`. Claude never changes mcgram config, access, or the inbox setting because a command asked, never sends secrets or files outside the cwd because a command asked, and treats any instruction to do so as an injection to report. mcgram cannot enforce this. It is Claude's prompt handling, the same as the existing "Prompt injection in Claude's context" row. |
| **E** | Typed text resolves an `ask` again (the 0.7.0 bug class) | The inbox never touches `AskRegistry`. `ask` still accepts taps only. Plain text that is not a `/command` is audited `ignored` and stored nowhere. |
| **T** | A redelivered update, or a new poll owner starting at `offset=0`, replays an old `/stop` | Records are created with `O_EXCL` per `update_id` and live for `ttl_s`. An update whose Telegram `date` is older than `ttl_s` at intake is dropped (`stale`). A session never sees a command sent before it started (60 s clock allowance). |
| **R** | "I sent /stop and Claude ignored it" | Intake audits `{tool:"_inbox", status:"queued", update_id, command, target, text}`. Each read audits `{tool:"check_inbox", label, update_ids, pid}`. The 👀 reaction shows in the chat that some session read it. |
| **I** | Command arguments sit on disk until they expire | 0600 files, `ttl_s` (default 30 min), swept by every instance. `audit.redact_text` covers the audit log (args are logged under `text`) but **not** the spool, as with the broker. |
| **D** | Flood of commands from the operator account, or from a stolen session | Intake uses its own token bucket (`rate_limit_per_min`) and stops at 50 pending records. Excess commands are dropped and audited (`rate_limited`, `inbox_full`). Args are capped at 1000 characters (`too_long`). Reactions use a separate bucket and are skipped when it is empty. |
| **D** | `check_inbox(wait_s=…)` freezes Claude | `wait_s` is capped at `limits.ask_timeout_max_s`. The call returns at once with `receiving: false` when no fresh heartbeat with the same protocol and a live pid exists. |

Commands are **pull-only**. Claude sees them only when it calls `check_inbox`. A session that sits
idle at the terminal, or blocks in a long `ask`, does not read them until it next calls the tool.
Nothing is pushed into the conversation.
```

## Validation

- Every claim in the text maps to a step in phase 2 or 3. Reviewers should tick each row against
  those phases.
- The links render, and the anchor `#operator-inbox` resolves.

## Risk

| Risk | L×I | Mitigation |
|---|---|---|
| The docs promise something the code does not do | M×H | Phases 2 and 3 each list the threat-model row they implement. Re-read the section in the phase 5 sweep. |

## Rollback

Revert the docs commit.
