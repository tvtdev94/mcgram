---
phase: 4
title: "Tool and server wiring"
status: pending
priority: P1
dependencies: [3]
effort: 2h
---

# Phase 4: Tool and server wiring

## Context

- `tools/ask.py:119-133` rejects every transport that is not Telegram with
  `transport_unsupported`. The schema description says "REQUIRES a telegram
  channel" (`:40`).
- The Telegram-only refusals in `tools/ask.py:135-141` → `_ask_refusal.refusal`
  (`tools/_ask_refusal.py:21-52`) concern poll ownership. They must **not**
  apply to ntfy.
- `AppState` (`runtime.py:18-48`) has one instance per process, created in
  `server._run` (`server.py:118-127`). The shutdown `finally` at
  `server.py:135-140` runs inside `async with _build_clients`, so the ntfy
  client is still open there.

## Files

- Modify: `src/mcgram/tools/ask.py`. Add the transport branch and update the
  schema description. The file stays under about 200 LOC.
- New: `src/mcgram/tools/_ask_ntfy.py`. It holds the ntfy validation, the call
  to `NtfyAsk.open`, the error mapping, and the audit (about 100 LOC).
- Modify: `src/mcgram/runtime.py`. Add the `ntfy_ask: Any | None = None`
  field, which is per process and set once at boot.
- Modify: `src/mcgram/server.py`. Construct the registry after the clients are
  built, and add the shutdown step.
- Modify: `tests/test_tools_ntfy.py`

## Steps

1. `server._run`: after constructing `AppState`, set
   `state.ntfy_ask = NtfyAsk(...)` only when `settings.ntfy` is set,
   `settings.ntfy.ask.enabled` is true, and `ntfy_client` is not None. In the
   `finally`, call `await state.ntfy_ask.shutdown()` before
   `_shutdown_phase3`, guarded by `if state.ntfy_ask`. It needs no lock and no
   ownership check, and every session gets one.
2. `tools/ask.py`, after the shared validation (`:89-110`) and
   `resolve_destination`:
   - When the transport is ntfy and `state.ntfy_ask` is set, return
     `await ask_ntfy(state, dest, question, options, effective_timeout,
     multi_select, columns)`.
   - Otherwise the unchanged `!= "telegram"` rejection applies. Add one hint
     for ntfy only: "ntfy ask is opt-in: see `ntfy.ask` in the threat model".
     The error code stays `transport_unsupported`.
3. `_ask_ntfy.ask_ntfy` checks the following in order. Each check fails fast,
   with no network call and an audit entry `status: "rejected"`:
   - `multi_select` → `invalid_input` / `multi_select_unsupported` with
     `transport: "ntfy"`.
   - `len(options) > 3` → `invalid_input` / `too_many_options` with `max: 3`.
     The cap is `min(limits.ask_options_max, 3)`, because ntfy allows "up to
     three user actions" ([publish docs](https://docs.ntfy.sh/publish/)).
   - Question over 4096 UTF-8 bytes → `invalid_input` / `question_too_long`.
     Past that size, ntfy turns the message into an attachment.
   - `not state.ntfy_client.has_token` → `ntfy_ask_requires_token`.
   - Server not `https://` → `ntfy_ask_requires_https`. This one is pending
     open question 2.
   - Rate limit: `state.rate.try_acquire("ask")`, with the same bucket and
     behaviour as Telegram (`tools/ask.py:143-145`).
   - Call `NtfyAsk.open`. Map the results:

     | Raised | Tool result |
     |---|---|
     | `NtfyTopicNotProtected` | `ntfy_topic_not_protected` (+ `topic`) |
     | `NtfyReplyUnreachable` | `ntfy_reply_unreachable` |
     | `NtfyAuthFailed` / `AuthError` | `ntfy_auth` |
     | `TransportError` | `transport_unreachable` (shape at `ask.py:161-168`) |
     | `NtfyError` | `ntfy_api` |

     Every error carries `channel` and a `hint` saying whether the question
     was posted.
   - `columns != 1` adds `note: "columns ignored on ntfy"`. This mirrors the
     `note` convention in `tools/_ntfy_options.py:75-76`.
   - Audit on success:
     `{tool:"ask", status:"ok", channel, transport:"ntfy", question_id, source, ms}`.
4. Update the schema description at `ask.py:29-41`. Say that ntfy channels
   work when `ntfy.ask` is enabled (up to 3 options, no `multi_select`).
   Otherwise the description stays as it is.

## Tests (`tests/test_tools_ntfy.py`; load `test-audit` first)

- Keep `test_ask_on_ntfy_returns_transport_unsupported` (`:148-158`). It is the
  disabled-path compatibility contract.
- Add a fixture variant with `ntfy.ask.enabled: true` and a token.
- **Table-driven pre-flight refusals.** Cover `multi_select`, 4 options, an
  oversize question, no token, and http. For each, assert the error code and
  that `httpx_mock.get_requests() == []`, which proves the refusal happened
  before any network call.
- **One end-to-end handler test.** Use the real `NtfyAsk` against mocked HTTP:
  probe 403/403, a stream with `open` and a reply, then the publish. Assert the
  tool result shape (`value`, `source`, `question_id`, `channel`) and the audit
  record (`transport: "ntfy"`, no token).
- **Telegram refusals never apply to ntfy.** Use a Telegram plus ntfy config
  in which this session does not own polling (`poll_owner_pid` set, no
  registry). An ntfy ask must still go through, and must not return
  `polling_not_owned`.
- Mapping `NtfyTopicNotProtected` to `ntfy_topic_not_protected` gets one case.
  Phase 3 owns the probe logic itself.

## Validation

`uv run pytest -q` (the full suite: the shared tool contract changed), then
`uv run ruff check src tests` and `uv run mypy src`.

## Risk and rollback

| Risk | Mitigation |
|---|---|
| The ntfy branch accidentally runs before the Telegram ownership checks for Telegram channels | The branch keys on `dest.transport == "ntfy"` only. The existing `tests/test_tools_ask.py` cases guard the Telegram path. |
| Shutdown ordering leaves the listener running after the client closes | Shutdown runs inside `_build_clients` (`server.py:118-140`). Phase 3's shutdown test asserts that the task is done. |

Rollback: revert this phase alone, which disconnects the feature. Phases 2 and
3 stay as inert code.
