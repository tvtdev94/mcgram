---
phase: 3
title: "ntfy ask core: registry and reply listener"
status: pending
priority: P1
dependencies: [2]
effort: 5h
---

# Phase 3: ntfy ask core

## Context

- The Telegram counterpart is `AskRegistry.open` (`src/mcgram/ask_registry.py:73-144`).
  It registers the question before posting, waits with `asyncio.wait_for`,
  marks the outcome in the chat, and on `CancelledError` marks the question
  stopped. That marking is bounded by `_ABANDON_EDIT_TIMEOUT_S = 5.0` (`:24`).
- The marker strings are inline at `ask_registry.py:222` (`✅ Đã chọn:`),
  `:244` (`⏱ timed out`) and `:261` (`⏹ mcgram stopped before an answer`).
- The backoff pattern to mirror is 1 s doubling to a 30 s cap
  (`src/mcgram/polling.py:19,74`).
- The broker's requester does one final read after the deadline for the same
  race (`src/mcgram/ask_broker.py:115-132`).

## Files

- New: `src/mcgram/ntfy_reply_listener.py` (about 150 LOC)
- New: `src/mcgram/ntfy_ask.py` (about 180 LOC)
- Modify: `src/mcgram/ask_registry.py`. Extract the three marker strings into
  module-level helpers (`answered_text`, `timed_out_text`, `stopped_text`) and
  use them at `:222`, `:244` and `:261`. Telegram behaviour is unchanged.
- New: `tests/test_ntfy_ask.py`

## `ReplyListener` (ntfy_reply_listener.py)

It has one job: keep a stream open on the reply topic while asked to, and hand
each `message` event to a callback.

The interface:

- `ensure_started()`
- `async wait_ready(timeout)`, which returns True once an `open` event has been
  seen on the current connection
- `async catch_up()`
- `async stop()`
- the `connected` property

The state has three parts:

- **Cursor.** It holds the id of the last `message` event seen. Before any
  message arrives, it holds the `time` of the first `open` event. That is the
  server's clock, so the local clock never enters a `since=` comparison, which
  was the 0.7.0 lesson.
- **First connect.** It sends no `since`, because the question is posted only
  after `open` arrives.
- **Reconnect.** It sends `since=<cursor>`. ntfy caches messages for 12 hours by
  default ([config docs](https://docs.ntfy.sh/config/)).

The loop has these steps:

1. Run `async for ev in client.stream(topic, since=cursor)`.
2. On `open`, set ready, mark the connection connected and reset the backoff.
3. On `message`, advance the cursor, then call `on_message(ev)` inside a
   try/except. A callback bug must not kill the stream.
4. Ignore `keepalive` and every other event type.

It handles these failures:

- **`AuthError`.** Call `on_fatal("ntfy_auth")`, audit
  `{tool:"_ntfy_reply", status:"error", reason:"auth"}`, and stop. No retry,
  because the token or the ACL is wrong.
- **`TransportError`, `NtfyError`, or a read timeout.** Mark the connection not
  connected, audit `{tool:"_ntfy_reply", status:"error", error}`, then sleep the
  backoff and retry. A laptop that sleeps ends up on this path.

`catch_up()` runs `client.poll(topic, since=cursor)`, feeds each message to
`on_message`, and advances the cursor. It returns False when the poll failed.

`stop()` cancels the task and awaits it.

## `NtfyAsk` (ntfy_ask.py)

The constructor is `NtfyAsk(client, *, server, reply_topic, audit)`, and it
keeps `_pending: dict[str, _Pending]`. Each `_Pending` holds the `future`, the
`options` and `tokens: list[str]`.

`async open(topic, question, options, timeout_s) -> dict` runs these steps:

1. **Probe.** Run `client.anonymous_read_denied` on `topic` and on
   `reply_topic`. If either returns False, raise
   `NtfyTopicNotProtected(topic)`. Nothing has been posted at that point.
2. **Register.** Set `q_id = "q_" + secrets.token_hex(4)` and
   `tokens = [secrets.token_urlsafe(16) for _ in options]`, store the pending
   entry, then call `listener.ensure_started()`.
3. **Wait for the listener.** If `await listener.wait_ready(min(10, timeout_s))`
   fails, drop the entry, stop the listener when it is idle, and raise
   `NtfyReplyUnreachable`. Nothing has been posted.
4. **Post the question.** Call `client.publish(topic, question, actions=[…])`
   with one action per option:
   `{"action":"http","label":opt,"url":f"{server}/{reply_topic}","method":"POST","body":json.dumps({"q":q_id,"t":tok}),"clear":True}`.
   Keep the returned `id` as the question's sequence id.
5. **Wait.** Run `await asyncio.wait({future}, timeout=timeout_s)`. Do not use
   `wait_for(future)`: on timeout, `wait_for` cancels the future, and the
   catch-up in step 6 must still be able to resolve it.
6. **On timeout:**
   1. Run `listener.catch_up()`.
   2. If the future is now done, take that answer. A tap that reached ntfy
      before mcgram finished counts, and the decision uses no clock
      comparison.
   3. If the catch-up failed **and** the listener was disconnected at the
      deadline, raise `NtfyReplyUnreachable`.
   4. Otherwise the result is `{"value": "", "source": "timeout"}`.
7. **On `CancelledError`:** mark the question stopped (best effort, bounded to
   5 s, no retry), then re-raise.
8. **Mark the outcome.** Publish `answered_text` or `timed_out_text` with
   `sequence_id=<question id>`, priority 2 (decision 4), and no actions.
   This is best effort, and failures are only debug-logged. On server 2.16.0 or
   later with Android 1.22.2 or later, clients replace the notification
   ([publish docs, "Updating + deleting notifications"](https://docs.ntfy.sh/publish/);
   [releases](https://docs.ntfy.sh/releases/)). Elsewhere the marker shows as a
   new notification.
9. **Clean up in `finally`.** Remove the pending entry. If no pending entries
   remain, call `await listener.stop()`.

`_on_message(ev)` checks each reply in this order:

1. Read `body = ev.get("message")`. If it is not a str or is over 1024 bytes,
   ignore it.
2. Parse the JSON. If that fails, or the result is not a dict with str `q` and
   str `t`, ignore it.
3. If `q` is not in `_pending` or the future is already done, ignore it
   silently. It belongs to another session or it is late.
4. Look for the index `i` where `secrets.compare_digest(t, tokens[i])` holds.
   If none matches, audit `{tool:"_ntfy_reply", status:"rejected",
   reason:"bad_token", question_id:q}`. Never audit the token or the body.
5. Otherwise call `future.set_result((options[i], "button"))`.

`_on_fatal(reason)` resolves every pending future with an exception carrying
the reason (`NtfyAuthFailed`).

`async shutdown()` cancels the waits, which triggers the stopped markers
bounded to 5 s, then stops the listener.

The exceptions (`NtfyTopicNotProtected`, `NtfyReplyUnreachable` and
`NtfyAuthFailed`) are defined in `ntfy_ask.py` as `MCGramError` subclasses.
Phase 4 maps them to tool errors.

## Data flow invariants

- No body text leaves `_on_message`. Only `options[i]`, the caller's own label,
  is returned.
- The question is posted only after the probe passes and the listener is ready.
  A failure before posting leaves nothing on the phone.
- A process runs at most one listener, and only while `_pending` is non-empty.

## Tests (`tests/test_ntfy_ask.py`)

These exercise the HTTP boundary through pytest-httpx. The stream is an
`IteratorStream`, or an `add_callback` that yields lines gated by an
`asyncio.Event`, so a test controls when a reply arrives. There are no live
sends. Load the `test-audit` skill before writing them.

Each test below is one contract:

1. **Happy path.**
   - Setup: the probe returns 403 twice, the stream sends `open` and then the
     right token for option 1, and the publish succeeds.
   - Expect: `value == options[1]` and `source == "button"`.
   - Expect: the published action bodies hold distinct tokens. The `url` is
     `server/reply_topic`, and no action has a `headers` field. That last check
     is the security contract that the action carries no credential.
2. **Rejected replies.** Table-driven over a wrong token, an unknown `q`, a
   body that is not JSON, and a body over 1 KiB.
   - Expect: none of them resolves. The question times out.
   - Expect: only the wrong token on a known `q` is audited, and the audit
     holds no token.
3. **Replay.** A valid reply for an earlier question that has already closed
   arrives during a new question. Expect it to be ignored.
4. **Two registries on one reply stream.** This stands in for two sessions.
   Each resolves only its own question.
5. **Public topic.** The probe returns 200, so `NtfyTopicNotProtected` is
   raised. Expect no publish request to have been made, so nothing is posted.
6. **Listener never ready.** The stream connect fails, so
   `NtfyReplyUnreachable` is raised and nothing is posted.
7. **Reconnect.**
   - Setup: the first stream ends with an error after `open`, and the second
     request is checked for `since=<open time or last id>`. The second stream
     delivers the reply.
   - Expect: the question resolves.
8. **Deadline catch-up.**
   - Setup: the stream is down. The `poll=1&since=` catch-up returns the
     reply.
   - Expect: the question resolves as `button`.
   - Variant: the catch-up fails too. Expect `NtfyReplyUnreachable`, not a
     timeout.
9. **Auth failure.** The stream returns 403 mid-ask, so the pending ask fails
   fast with `NtfyAuthFailed`, well before its timeout.
10. **Shutdown.** The ask is cancelled, the stopped marker is published, and
    the listener task is done afterwards. No task is left pending.

These tests do not include a "listener stops when idle" check that asserts on
private state. Test 10 covers that observably, through the absence of a further
stream request.

## Validation

`uv run pytest tests/test_ntfy_ask.py tests/test_ask_registry.py tests/test_tools_ask.py -q`.
The Telegram tests prove that extracting the marker helpers is behaviour
neutral. Then run `ruff` and `mypy`.

## Risk and rollback

| Risk | Mitigation |
|---|---|
| Stream tests are flaky because of timing | Gate replies on events rather than sleeps, and keep `pytest-timeout` on. |
| The real ntfy `open` event lacks `time` | Fall back to a cursor of `since=<local now - 60s>`. Old replies are harmless, because ids and tokens are random. Verify in the phase 6 smoke test. |

No caller exists yet, so `git revert` is safe.
