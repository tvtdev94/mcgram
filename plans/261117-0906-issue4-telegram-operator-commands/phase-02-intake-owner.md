---
phase: 2
title: "Intake on the poll owner"
status: completed
priority: P1
dependencies: [1]
effort: 4h
---

# Phase 2: Intake on the poll owner

## Context

- `poll_loop` filters on `chat.id` only, then dispatches every surviving update to every handler
  (`polling.py:40-48`). `from_operator` also passes `edited_message` (`update_dispatcher.py:43`).
- `poll_loop` starts at `offset = 0` each time it is (re)started (`polling.py:34`). A new owner
  therefore receives every update that is still unconfirmed, up to Telegram's 24 h retention. This
  is why both the dedupe and the stale check are needed.
- The dispatcher only receives updates while this process polls (`poll_ownership.py:142-155`). A
  handler registered once at boot therefore runs only on the owner. No change to `PollOwnership` is
  needed.
- These parts are reused as they are:
  - `Spool` and `write_exclusive`, `sweep`, and the `u<digits>` key form
    (`spool.py:34-35, 138-172, 225-265`);
  - `safe_spool` (`broker_wiring.py:228-236`);
  - `RateLimiter.try_acquire` (`rate_limiter.py:37-45`);
  - `AuditLog.write`, which redacts only the `text` key (`audit.py:69-73`).
- The model is the broker's owner supervisor: it beats while it owns and withdraws on stop
  (`broker_wiring.py:282-329`).

## Files

| Action | File |
|---|---|
| Modify | `src/mcgram/config.py`: add `InboxConfig` and the `Settings.inbox` field (next to `BrokerConfig`, `:169-186` / `:225`) |
| Modify | `src/mcgram/spool.py`: add a shared `heartbeat_status()` helper |
| Modify | `src/mcgram/ask_broker.py`: `owner_alive` (`:50-80`) delegates to the helper. Reason strings stay identical |
| Modify | `src/mcgram/runtime.py`: add the `inbox: Any \| None = None` field (`AppState`, `:145-177`) |
| Modify | `src/mcgram/server.py`: call `start_inbox(state)` after `start_ask_broker` (`:131`) and cancel it in `finally` (`:135-140`) |
| Create | `src/mcgram/inbox.py`: parse and filter, sanitize, HMAC, `InboxStore`, intake handler |
| Create | `src/mcgram/inbox_wiring.py`: boot checks, `InboxSession`, heartbeat and sweep supervisor |
| Create | `tests/test_inbox.py` |

## Requirements

### Config

`InboxConfig` has two fields:

- `enabled: bool = False`
- `ttl_s: int = Field(1800, ge=60, le=86400)`

Everything else is a module constant in `inbox.py`:

- `PROTOCOL_VERSION = 1`
- `MAX_PENDING = 50`
- `TEXT_MAX_CHARS = 1000`
- `CLOCK_ALLOWANCE_S = 60`
- `HEARTBEAT_KEY = "u0"`, with kind `heartbeat`

The heartbeat timing reuses `broker.heartbeat_stale_s`, `sweep_interval_s`, and `tmp_grace_s`,
which is what reminders already do (`broker_wiring.py:250-252`).

### Parse and filter (`parse_update(update, operator_chat_id)`)

The function returns either a `Command` or a `(status, reason)` pair. The handler audits the
reason. Checks run in this order, and the first failure wins:

1. Update without `message`: ignored silently, not audited. These are callback queries for `ask`,
   so auditing them would add noise.
2. `edited_message`: `ignored/edited`.
3. `chat.id != operator_chat_id`: `rejected/non_operator`. This is defense in depth, because
   `poll_loop` already drops these.
4. `chat.type != "private"`: `rejected/not_private`.
5. `from.id != operator_chat_id`, or `from.is_bot`, or `via_bot` present:
   `rejected/sender_mismatch`.
6. Any of `forward_origin`, `forward_date`, `is_automatic_forward`: `rejected/forwarded`.
7. No `text` (photo, sticker, caption only): `ignored/not_text`.
8. Text that does not match `^/([A-Za-z0-9_]{1,32})(?:@([A-Za-z0-9_]{1,32}))?(?:\s+(.*))?$` (DOTALL):
   `ignored/not_command`.
9. Command `start`, which Telegram sends on first open: `ignored/reserved`.
10. Args longer than `TEXT_MAX_CHARS`: `dropped/too_long`.

On success the function produces a `Command` with these fields:

- `update_id`, `message_id`, `chat_id`;
- `command` (lowercased) and `target` (lowercased, or `None`);
- `args`, sanitized: Unicode categories Cc and Cf are removed, except `\n` and `\t`;
- `sent_at`, from `message.date`.

### Store (`InboxStore`)

**Record.** Each command is stored at `u<update_id>.cmd.json` with these fields:

- `protocol`, `update_id`, `message_id`, `chat_id`;
- `command`, `target`, `args`;
- `sent_at`, `received_at`, `received_by_pid`;
- `expires_at = received_at + ttl_s`;
- `sig`.

**Signature.** `key = hmac(bot_token, b"mcgram-inbox")`.
`sig = hmac_sha256(key, json.dumps(record_without_sig, sort_keys=True, separators=(",", ":"), ensure_ascii=False))`.
Compare with `hmac.compare_digest`.

**`publish(cmd, now)`** runs these steps in order:

1. When `now - sent_at > ttl_s`, drop the command (`stale`).
2. When `len(keys("cmd")) >= MAX_PENDING`, drop it (`inbox_full`).
3. Otherwise call `write_exclusive`. `False` means a duplicate: write a debug log and no audit
   record.

**Invariant.** Because `expires_at >= sent_at + ttl_s`, any redelivery that arrives after the record
expired is already `stale`, so dedupe cannot be bypassed by sweep timing.

**`unread(seen, label, since, now)`** returns verified records in `update_id` order and skips:

- a key in `seen`;
- an unparseable file. It is not added to `seen`, because `write_exclusive` can expose a partial
  file (`spool.py:141-146`);
- a protocol mismatch;
- a bad signature. This key is returned separately so the caller can audit it once and add it to
  `seen`;
- `chat_id != operator`;
- `sent_at < since - CLOCK_ALLOWANCE_S`;
- `expires_at < now`;
- a `target` that is set and differs from `label`.

**Heartbeat.** `beat()` writes `{pid, at, protocol, expires_at}`. `withdraw()` removes the heartbeat
only when its `pid` is this process. `receiving()` returns
`heartbeat_status(read(HEARTBEAT_KEY), protocol, stale_s)`.

### Shared heartbeat helper

`spool.heartbeat_status(hb, *, protocol, stale_s, what)` returns `(alive, reason)`. It moves the
body of `ask_broker.py:63-80` without changing behavior; `what` fills the word "broker" in the
messages. `AskBroker.owner_alive` keeps its own `None` message (`:61-62`) and delegates the rest.

### Wiring (`inbox_wiring.start_inbox(state) -> asyncio.Task | None`)

1. If `inbox.enabled` is off, return `None` and leave `state.inbox` as `None`.
2. Otherwise build `InboxSession(label, started_at=time.time(), seen=set(), store, unavailable)`.
   `unavailable` holds the first reason that applies:
   - no Telegram client;
   - `bot.disable_polling`;
   - `operator_chat_id <= 0` (logs a warning);
   - an unsafe spool (`safe_spool(settings, "inbox")`).
3. When `store` is set, register the intake handler once with `state.dispatcher.register`, then
   start the supervisor:
   - while `state.owns_polling` (`runtime.py:174`), call `beat()` every
     `min(10, heartbeat_stale_s / 6)`;
   - when ownership is lost and at cancel, call `withdraw()`;
   - every `sweep_interval_s`, call `spool.sweep(tmp_grace_s=…)`, in every instance;
   - prune `seen` down to keys still on disk.

**Label.** Use `MCGRAM_SESSION_LABEL` when it is set, otherwise `Path.cwd().name`. Lowercase it,
replace every character outside `[a-z0-9_]` with `_`, trim to 32 characters, and fall back to
`session` when the result is empty. Log it at boot.

### Intake handler

Order: `parse_update`, then `state.rate.try_acquire("_inbox_intake")` (on failure,
`dropped/rate_limited`), then `publish`. Every outcome except a silent ignore or a duplicate writes
an audit record:

```
{tool: "_inbox", status, reason?, update_id, message_id?, command?, target?, text: args?}
```

The handler makes no network call; it must never stall `getUpdates`.

## Tests (`tests/test_inbox.py`, real `poll_loop` + real `Spool` in tmp_path)

Load the `test-audit` skill first. One primary owner for each contract:

1. **Filter table.** Drive the real `poll_loop` with the `FakeClient` pattern
   (`tests/test_polling.py:23-38`). Use one batch per case and parametrize over: accepted DM
   command, group chat, sender mismatch, bot sender, `via_bot`, forwarded, edited, `not_command`,
   `/start`, too long. Assert the record exists only for the accepted case and that the audit
   reason matches. Regression this catches: a filter step lost in a refactor.
2. **Dedupe across owner restart.** Dispatch the same `update_id` twice: one record and no second
   `queued` audit.
3. **Stale and capacity.** `sent_at` older than `ttl_s` gives `stale`. 50 existing records give
   `inbox_full`. The 21st command in a minute gives `rate_limited`.
4. **Supervisor.** Flip `state.owns_polling` and check that the heartbeat appears and then is
   withdrawn. Check that an expired record is swept. Model this on the broker supervisor tests.
5. **Unavailable reasons.** With a negative `operator_chat_id`, `state.inbox.unavailable` is set and
   no handler is registered.
6. **No new test for `heartbeat_status`.** The existing `tests/test_ask_broker.py:82-96` and
   `tests/test_ask_broker_e2e.py:255-259` own that contract and must pass unchanged.

The signature and the `unread` filters are owned by the phase 3 tool-boundary tests. They are not
duplicated here.

## Validation

`uv run pytest tests/test_inbox.py tests/test_ask_broker.py tests/test_ask_broker_e2e.py tests/test_polling.py`,
then `uv run ruff check src tests` and `uv run mypy src`.

## Risk

| Risk | L×I | Mitigation |
|---|---|---|
| A refactor of the heartbeat helper changes the broker's behavior | L×H | Move the code as it is. The existing broker tests are the gate. The helper can be reverted on its own |
| File I/O in the poll-loop handler slows intake | L×M | One `listdir` plus one small write per command. The handler makes no network call |
| A clock ahead or behind by more than 60 s hides fresh commands (`since` floor) | L×M | Document it. `received_at` and `started_at` share one local clock; only `sent_at` uses Telegram's |
| cwd is not the project dir under some MCP launchers | M×L | `MCGRAM_SESSION_LABEL` overrides it, and `check_inbox` returns the label so Claude can tell the operator. **[UNVERIFIED]**: the cwd that Claude Code uses for stdio servers. Tests choose their own cwd and cannot prove it, so phase 5 adds a manual smoke step for it |

## Rollback

Revert the commit. `inbox.enabled` defaults to off, so nothing else depends on these modules.
