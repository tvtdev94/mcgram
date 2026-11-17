---
title: "ask over ntfy via action buttons and a reply topic"
description: "Opt-in two-way ask on ntfy: http action buttons post a per-option token to a reply topic that mcgram subscribes to; requires an access token and read-protected topics."
status: in-progress
priority: P2
effort: 14h
branch: feat/ntfy-ask
tags: [ntfy, ask, security, transport]
created: 2026-11-17
---

# ask over ntfy (GitHub issue #3)

## Outcome

With `ntfy.ask.enabled: true`, `ask` on an ntfy channel posts the question with
up to three http action buttons. A tap makes the ntfy app POST a per-option
token to a reply topic. mcgram subscribes to that topic for as long as a
question is open, matches the token to the question, and returns
`{value, source: "button", question_id}` (or `source: "timeout"`). The contract
stays as 0.7.0 defined it: `options` is required, answers come only from button
taps, and `value` is always one of the caller's labels.

## Non-goals

- `multi_select` on ntfy. ntfy has no toggle UI, so it returns `invalid_input`.
- More than three options on ntfy, because ntfy allows at most three actions.
- Free-text answers, or any reply body text reaching Claude.
- Carrying ntfy asks through the broker or the spool. They are not needed (see
  the multi-session section).
- Discord `ask`, which stays `transport_unsupported`.
- Any change to the Telegram `ask` path beyond sharing marker strings.
- Embedding an ntfy credential in the notification.

## Acceptance criteria

1. With `ntfy.ask` absent or disabled, an ntfy `ask` returns the same
   `transport_unsupported` as 0.7.0 and makes no network calls.
   `tests/test_tools_ntfy.py::test_ask_on_ntfy_returns_transport_unsupported`
   passes unchanged.
2. With the feature enabled, the right per-option token on the reply topic
   resolves the question with that option's label. A wrong token, an unknown
   `q`, or a malformed body never does.
3. A replayed reply cannot resolve a later question.
4. A reply-topic body is never returned to the caller, logged, or audited.
5. `ask` refuses before posting in each of these cases, with a distinct error:
   no token, http server, either topic anonymously
   readable, or listener not ready.
6. A tap made while the listener was disconnected is still delivered if the
   listener reconnects or the catch-up read succeeds before the deadline.
7. If the reply topic stayed unreachable through the deadline, `ask` returns
   `ntfy_reply_unreachable`, not `source: "timeout"`.
8. A 401 or 403 on the subscription fails every pending ntfy ask at once with
   `ntfy_auth`.
9. Several sessions can each have an ntfy ask open at the same time without
   cross-resolving. No poll lock or broker is involved.
10. Shutdown cancels the listener and marks open questions as stopped, bounded
    to 5 seconds. Nothing is left running.
11. The token and option tokens never appear in audit, logs or tool results.
12. `uv run pytest`, `uv run ruff check`, and `uv run mypy src` all pass.
13. The threat-model entry is merged before any code.

## Design summary

```
Claude ─ask(q, options≤3)─▶ tools/ask.py ─ntfy─▶ tools/_ask_ntfy.py ─▶ NtfyAsk.open()
  1. probe: anonymous GET /<question_topic>/json?poll=1 and /<reply_topic>/json?poll=1 → must be 401/403
  2. register Pending{q_id, tokens[i], future}; ReplyListener.ensure_started(); wait for `open` event
  3. POST <server>/  {topic, message, actions:[{action:"http", url:<server>/<reply_topic>,
                     method:"POST", body:'{"q":"q_ab12cd34","t":"<token_i>"}', clear:true}×n]}
Phone tap ─POST (anonymous)─▶ <server>/<reply_topic>   (topic: everyone write-only)
ReplyListener ◀─GET /<reply_topic>/json?since=<cursor> (Bearer token, read timeout 90s)─ ntfy
  message event → NtfyAsk._on_message → pending[q] → compare_digest(t, tokens[i]) → future.set_result
  4. deadline: one catch-up GET ...?poll=1&since=<cursor>; then outcome
  5. outcome marker: POST <server>/ {topic, sequence_id:<question id>, message:"Q\n\n✅ …"} (best effort)
```

The key decisions follow.

- **The reply topic is anonymously writable and the action carries no
  credential.** ntfy access tokens "grant users full access to the user
  account"; granular tokens are not implemented
  ([config docs](https://docs.ntfy.sh/config/)). Embedding the token in an
  action would publish the whole account to every reader of the notification.
  On ntfy.sh this is a reserved topic with everyone set to write-only. When
  self-hosting it is `ntfy access everyone <reply_topic> write-only`.
- **Answers are authenticated with per-option tokens.** Each option gets its
  own 128-bit token (`secrets.token_urlsafe(16)`). Seeing one reply reveals
  only the chosen option's token, so an observer of the reply cannot swap in
  another option.
- **Answers arrive over a JSON stream, not repeated polling.** ntfy.sh limits
  requests to a burst of 60, refilled one every 5 s
  ([config docs](https://docs.ntfy.sh/config/)), so polling every second or two
  would exhaust the budget. A single stream costs one request. Its keepalive is
  45 s by default and must be under the Android app's 77 s timeout, so a 90 s
  read timeout detects dead connections.
- **Subscribe before publishing.** mcgram waits for the stream's `open` event
  before it posts the question, so a fast tap is never missed. The
  [anotifier PR #96](https://github.com/DevinoSolutions/anotifier-for-claude-codex-cursor/pull/96)
  does the same.
- **Check that the topics are protected on every ask.** Before posting, mcgram
  makes two cheap anonymous GETs. A valid token on ntfy.sh says nothing about
  whether a topic is reserved, so the token alone is not proof.
- **No broker.** ntfy fans every message out to every subscriber, so each
  session subscribes on its own and ignores `q` ids it did not open.

## Phases

| # | Phase | Depends on | Effort | Files owned |
|---|---|---|---|---|
| 1 | [Threat-model entry](phase-01-threat-model-entry.md) (gate) | none | 1h | `docs/security-threat-model.md` |
| 2 | [Config and ntfy client primitives](phase-02-config-and-ntfy-client.md) | 1 | 3h | `config.py`, `ntfy_client.py`, `tests/test_config_ntfy.py`, `tests/test_ntfy_client.py` |
| 3 | [ntfy ask core: registry and reply listener](phase-03-ntfy-ask-core.md) | 2 | 5h | new `ntfy_ask.py`, new `ntfy_reply_listener.py`, `ask_registry.py` (marker helpers only), new `tests/test_ntfy_ask.py` |
| 4 | [Tool and server wiring](phase-04-tool-and-server-wiring.md) | 3 | 2h | `tools/ask.py`, new `tools/_ask_ntfy.py`, `runtime.py`, `server.py`, `tests/test_tools_ntfy.py` |
| 5 | [doctor checks](phase-05-doctor-checks.md) | 2 (can run in parallel with 3 and 4) | 1.5h | `cli_doctor.py`, `tests/test_cli_doctor_ntfy.py` |
| 6 | [Docs, examples, skill, CHANGELOG](phase-06-docs-and-changelog.md) | 4, 5 | 1.5h | `README.md`, `docs/architecture.md`, `docs/manual-smoke-test.md`, `CHANGELOG.md`, `config.example.yaml`, `src/mcgram/data/config.example.yaml`, `src/mcgram/data/skill/SKILL.md` |

No two phases that may run in parallel touch the same file.

## Config shape

```yaml
ntfy:
  server: https://ntfy.example.com     # https required for ask (decision 1)
  default_topic: mcgram-q-<hex>        # question topic: anonymous read DENIED
  access_token_env: NTFY_TOKEN         # REQUIRED when ask.enabled
  ask:
    enabled: false                     # opt-in; default off
    reply_topic: mcgram-r-<hex>        # everyone write-only; read only by the token's user
```

## Backwards compatibility and rollback

- The feature is off by default. The disabled path returns the identical
  error, and the change adds only new error codes. Pydantic ignores unknown
  keys by default, so an older mcgram reading a config with `ntfy.ask` behaves
  exactly as before. That makes a downgrade safe.
- Each phase reverts cleanly with `git revert`. Phases 2 and 3 are additive
  modules, and the phase 4 wiring is the only switch that activates them.
- The release is a minor version bump (0.8.0) under "Added".

## Risks

| Risk | L×I | Mitigation |
|---|---|---|
| A user enables the feature on a public topic | M×H | The per-ask anonymous-read probe refuses (`ntfy_topic_not_protected`), and doctor shows it. |
| iOS ignores `clear=true` ([ntfy#1728](https://github.com/binwiederhier/ntfy/issues/1728)) and brings the app to the foreground on an http tap | H×L | The outcome marker uses `sequence_id` to replace the notification. The behaviour is documented. Later taps are ignored. |
| Older server (< 2.16.0) or app (< 1.22.2) cannot update a notification ([releases](https://docs.ntfy.sh/releases/)) | M×L | The marker shows up as a new, low-priority notification. It is best effort. |
| Mocks diverge from real ntfy behaviour (codes, `open` event, wire format of the action body) | M×M | Phase 6 adds a manual smoke test against a self-hosted ntfy and a real phone. `ntfy_client.py:32` already flags this gap. |
| Laptop sleep or a network outage during an ask | M×M | The listener reconnects with `since=<cursor>`, reads once more at the deadline, and returns `ntfy_reply_unreachable` rather than `timeout`. |

## Decisions (approved by the user, 2026-11-17)

The plan and the phase 1 threat-model text were approved as proposed. The open
questions were answered as follows.

1. **http servers.** `ask` requires an `https://` server and refuses otherwise
   with `ntfy_ask_requires_https`. `mcgram doctor` reports an `http://` server
   as `[FAIL]` when `ntfy.ask` is on.
2. **Probe strictness.** The anonymous-read probe is strict: `ask` refuses with
   `ntfy_topic_not_protected`, and there is no override.
3. **Reply-topic auth model.** No write-only-token-in-button mode. It stays a
   non-goal.
4. **Outcome marker priority.** The replacement notification uses priority 2
   (no sound).
5. **Stale SKILL.md row.** The `SKILL.md:25` "ask without options" fix ships in
   a separate PR (#13). This plan does not touch that line.

## Implementation notes (deviations from the phase text)

- **Exception names** carry an `Error` suffix (`NtfyTopicNotProtectedError`,
  `NtfyReplyUnreachableError`, `NtfyAuthFailedError`, base `NtfyAskError`) to
  match the repo's exception naming and ruff's N818. `posted` on the base class
  tells the tool layer whether the question reached the phone.
- **Probe query** is `?poll=1&since=none` instead of `since=latest`, so a
  readable topic hands back no cached message. `none` is a documented value of
  ntfy's `parseSince` (`server/server.go`).
- **Verified against ntfy source:** the topic charset `^[-_A-Za-z0-9]{1,64}$`
  (`topicRegex`, `server/server.go`); an anonymous read of a denied topic
  answers 403 (`authorizeTopic` → `errHTTPForbidden`,
  `server/server_middleware.go`); the `open` event carries `id` and `time`
  (`model.NewMessage`). Both 401 and 403 still count as denied.
- **Cursor fallback.** If an `open` event ever lacks `time`, a reconnect or a
  catch-up uses `since=all`. That is safe because ids and tokens are random.
- **Threat model** gained one bullet under "Required setup" for the https
  requirement (decision 1); the rest is the approved text.
- **Root `config.example.yaml`** has no `ntfy:` section, so only the bundled
  `src/mcgram/data/config.example.yaml` (what `mcgram init` writes) got the
  commented `ask:` block.
- **iOS wording.** ntfy#1728 says iOS ignores `clear` and leaves the tapped
  notification in place; it does not say the app opens. The docs say that.
