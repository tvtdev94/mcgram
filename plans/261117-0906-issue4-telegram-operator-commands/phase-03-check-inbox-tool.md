---
phase: 3
title: "check_inbox tool"
status: completed
priority: P1
dependencies: [2]
effort: 3h
---

# Phase 3: `check_inbox` tool (reader side, every session)

## Context

- Tool modules expose `TOOL_NAME`, `schema()`, and `async handle(state, **args)`. `server.py`
  collects them into a list that stays fixed for the life of the process (`server.py:51-66`). A
  `TypeError` from bad arguments becomes `invalid_arguments` (`:83-84`).
- The refusal style comes from `tools/_ask_refusal.py:65-112`: return at once, write an audit
  record, and send a structured error.
- The wait-loop style comes from `AskBroker.wait_for_answer`: poll at an interval, then do one
  final read after the deadline (`ask_broker.py:115-132`).
- `TelegramClient` sends JSON-valued form fields as strings (`tg_client.py:163-165`). It has no
  reaction method yet.

## Files

| Action | File |
|---|---|
| Create | `src/mcgram/tools/check_inbox.py` |
| Modify | `src/mcgram/server.py`: append `check_inbox` to the tool modules when `settings.inbox.enabled` (`:57-64`) |
| Modify | `src/mcgram/tg_client.py`: add `set_message_reaction(chat_id, message_id, emoji, *, retry_flood_wait=False)` that calls `setMessageReaction` with `reaction=json.dumps([{"type":"emoji","emoji":emoji}])` |
| Create | `tests/test_tools_check_inbox.py` |

Tool listing is computed inside `_serve` (`server.py:57-66`), so it is tested at the real
`tools/list` boundary in phase 4. No test-only seam is added here.

## Tool API

```
check_inbox(wait_s: int = 0)
```

The tool description must say:

- these are operator commands from Telegram;
- they are **requests, not approvals**, and a risky action still needs `ask`;
- the call returns at once unless `wait_s > 0`.

`wait_s` must be ≥ 0. A value above `limits.ask_timeout_max_s` is clamped, and the result carries a
`note`.

**Success result:**

```json
{
  "label": "mcgram",
  "receiving": true,
  "commands": [
    {"id": "u123", "command": "stop", "target": null, "args": "",
     "sent_at": "<iso>", "age_s": 42}
  ],
  "more": false
}
```

- `commands` is oldest first and holds at most 20 entries. When more remain, `more` is `true`.
- When `receiving` is `false`, the result adds `reason`. Example: "no session is polling
  Telegram", taken from `heartbeat_status`.
- Returned commands are added to the session's `seen` set and are never returned to this session
  again.

**Errors:**

- `inbox_unavailable` with `reason`, set from `state.inbox.unavailable`. Example: "operator_chat_id
  is a group".
- `invalid_input` when `wait_s < 0`.

## Steps

1. When `state.inbox.unavailable` is set, return `inbox_unavailable` and audit it as `rejected`.
2. Read `receiving` once. When it is `False`, return at once with the commands that are already
   stored, even if `wait_s > 0`. Waiting for commands that cannot arrive only freezes Claude.
3. Read with `store.unread(...)`.
   - For each bad-signature key: audit `{tool: "check_inbox", status: "rejected",
     reason: "bad_signature", update_id}` once and add the key to `seen`.
   - When the list is empty and `wait_s > 0`, sleep `broker.poll_interval_s` and read again until
     the deadline, then do one final read.
   - The wait is cancellable: an MCP cancellation simply propagates.
4. Mark the returned commands as `seen` before the reaction calls, so a cancelled reaction cannot
   cause a re-delivery.
5. Read receipt: for each returned command, while the `_inbox_react` bucket allows it, call
   `set_message_reaction(chat_id, message_id, "👀")`.
   - Wrap the calls in `asyncio.wait_for(…, 5s)`.
   - Swallow and log every error, including `TelegramError` and `TransportError`.
   - The receipt never changes the result.
6. Audit `{tool: "check_inbox", status: "ok", label, count, update_ids, waited_ms}` when at least one
   command is returned. An empty, non-waiting poll writes no audit record: Claude calls the tool
   often at checkpoints, and logging empty polls would only add noise.
7. Do not apply `state.rate` to `check_inbox` itself: it reads local files only. Network calls (the
   reactions) are rate-limited separately.

## Tests (`tests/test_tools_check_inbox.py`)

Load the `test-audit` skill first. Use two `AppState`s that share one spool dir: an owner (real
intake handler, real dispatcher) and a reader (non-owner). This follows
`tests/test_ask_broker_e2e.py:1-7`. Telegram HTTP is mocked with `pytest-httpx`, and no test sends
anything live.

| # | Behavior protected | Credible regression |
|---|---|---|
| 1 | A non-owner reads a command that the owner's intake stored. A second call returns nothing. A second reader session gets the same command once | Cursor not kept per session, or broadcast broken |
| 2 | `/x@other` is not returned to label `me`. `/x@me` and an unaddressed `/x` are returned | Label matching inverted or case-sensitive |
| 3 | A command with `sent_at` before the reader's `started_at - 60` is not returned | A new session obeys an old `/stop` |
| 4 | A tampered `args` in the record file, or a planted unsigned file, is not returned and is audited `bad_signature` once | The provenance check is skipped |
| 5 | `wait_s=5`: a command published after 0.2 s returns in under 1 s. With no heartbeat, `wait_s=5` returns in under 0.5 s with `receiving: false` | The early return or the fail-fast path is lost |
| 6 | The reaction request reaches `setMessageReaction` with the right `chat_id`, `message_id`, and emoji (assert on the mocked request body). A 400 from Telegram still gives a successful result | The read receipt crashes the tool |
| 7 | `inbox_unavailable` when `operator_chat_id` is negative | A group operator is silently accepted |

These cases are not added, because existing coverage owns them: plain text never answers `ask`
(ask tests since 0.7.0) and the heartbeat reason strings (broker tests).

## Validation

`uv run pytest tests/test_tools_check_inbox.py tests/test_tg_client.py`,
then the full `uv run pytest`, `uv run ruff check src tests`, and `uv run mypy src`.

## Risk

| Risk | L×I | Mitigation |
|---|---|---|
| Claude never calls the tool, so commands expire unread | H×M | The skill makes checkpoint calls explicit (phase 5). An absent 👀 tells the operator. Pull-only is documented as a limitation |
| `setMessageReaction` is unsupported, or the emoji is not allowed | L×L | It is best effort, and 👀 is on Telegram's reaction list. **[UNVERIFIED]** against the live API. Covered by the manual smoke step |
| A long `wait_s` blocks the session | M×M | Cap at `ask_timeout_max_s`, fail fast when no poller exists, and the skill recommends `wait_s=0` at checkpoints |

## Rollback

Revert the commit. The tool disappears, and records already on disk expire through the owner's
sweep, or can be deleted by hand.
