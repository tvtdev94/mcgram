---
phase: 1
title: "Threat-model entry for ask over ntfy"
status: pending
priority: P1
dependencies: []
effort: 1h
---

# Phase 1: Threat-model entry (gate)

## Context

The issue requires a threat-model entry before implementation. Today the doc
lists ntfy as outbound only (`docs/security-threat-model.md:10-14`), and its
Spoofing table is Telegram and broker only (`:27-34`). This phase changes
documentation only. **Gate:** phases 2 to 6 do not start until the user
approves this text.

## Files

- Modify: `docs/security-threat-model.md`. This phase owns only that file.

## Steps

1. Trust boundaries (`:9-23`). Add `ntfy reply topic (inbound, only with
   ntfy.ask.enabled)` to the diagram. Extend the "Adversary" line with "anyone
   who can publish to the ntfy reply topic".
2. Add the section below after "Broker spool" (`:94-113`) and before
   "Reporting issues".
3. Add one bullet to "What mcgram does NOT defend against" (`:84-92`): "With
   `ntfy.ask` on, the ntfy server operator, or anyone who compromises the ntfy
   account, can answer an `ask`."

## Proposed text

```markdown
## ntfy ask (reply topic)

Off by default (`ntfy.ask.enabled: false`). With it on, `ask` works on ntfy
channels: the question carries up to three `http` action buttons, and a tap
makes the ntfy app POST to a **reply topic** that mcgram subscribes to while a
question is open.

**Who can answer.** Anyone who can *read the question topic* (the buttons carry
the answer tokens) and *publish to the reply topic*. With the required setup
that is: devices logged in to your ntfy account, the ntfy server operator, and
anyone holding your ntfy token or password. On Telegram the equivalent set is
"whoever controls `operator_chat_id`".

**Required setup — enforced, not advisory.**
- `ntfy.access_token_env` set. Without it the config fails to load; with the
  env var unset at runtime, `ask` returns `ntfy_ask_requires_token` and posts
  nothing.
- Question topic: anonymous read **denied** (ntfy.sh: reserved, "only I can
  publish and subscribe"; self-hosted: `auth-default-access: deny-all`).
- Reply topic: anonymous **write-only** (ntfy.sh: reserved, everyone
  write-only; self-hosted: `ntfy access everyone <reply_topic> write-only`),
  read only by your user.
- Before every ntfy `ask`, mcgram makes an anonymous read of both topics and
  refuses (`ntfy_topic_not_protected`) unless both answer 401/403. A token alone
  proves nothing: on ntfy.sh a valid token works on public topics too.

**Why the button carries no credential.** An ntfy access token grants full
access to its account; scoped tokens do not exist. A token inside an action
would reach every device and cache that holds the notification. So the reply
topic accepts anonymous posts, and answers are authenticated by tokens instead.

| STRIDE | Threat | Mitigation |
|---|---|---|
| **S** | Someone who knows or guesses the reply topic posts an answer | Each option has its own random 128-bit token, shown only inside the read-protected question. An answer must name an open question id **and** match one of its tokens (`secrets.compare_digest`). A known id with a wrong token is audited `{tool:"_ntfy_reply", status:"rejected", reason:"bad_token"}`. |
| **S** | Someone reads the question topic and answers | Prevented by the read-protection probe before each ask. If the topic is public, `ask` refuses and posts nothing. |
| **S** | The ntfy server, or whoever runs it, forges an answer | **Not defended**, the same as Telegram's servers forging a button tap. For approvals that matter, use a server you control. |
| **T** | Replay an earlier reply (from the ntfy cache or a captured request) | Tokens are single-use: the question is removed on its first valid answer, its timeout, or shutdown. Question ids are random per question, so a cached reply from an earlier question matches nothing that is open. |
| **T** | Someone who saw one reply (proxy log, reply-topic leak) changes the chosen option | Tokens are per option. A reply reveals only the chosen option's token. |
| **I** | Account token leaks through the notification | It never enters it (see above). The token stays in `~/.mcgram/.env` and goes out only as the `Authorization` header on mcgram's own requests. Neither the token nor the answer tokens are written to audit, logs or tool results. |
| **I** | Question text stays on the ntfy server (12h cache by default) and on devices | Same as `send_message` over ntfy. Don't ask about secrets. |
| **I** | Tokens sniffed on a plain-HTTP server | `ask` requires an `https://` server *(pending decision)*. |
| **D** | Flood the reply topic, which anyone can post to | Bodies over 1 KiB are dropped before parsing, and malformed or unknown-id bodies are ignored without an audit entry. ntfy rate-limits the publisher by IP. A flood can delay or hide an answer (replay is capped at 10 MB per topic). It cannot forge one. |
| **D** | Subscription drops (sleep, network) and an answer is missed or reported as "ignored" | The listener reconnects with `since=<last id>` and reads once more at the deadline. If the reply topic was unreachable, `ask` returns `ntfy_reply_unreachable`, never `source:"timeout"`. A 401/403 on the subscription fails every open ntfy ask at once with `ntfy_auth`. |
| **E** | Prompt injection through the answer | The reply body never reaches Claude. `value` is always one of the labels the caller passed, chosen by token match. A prompt-injected Claude can still ask a leading question, the same as on Telegram. |

**Several sessions.** No poll lock, no broker, no spool. ntfy delivers every
reply to every subscriber, so each session subscribes on its own while it has a
question open and ignores ids it did not create (silently: they belong to
another session). `broker.enabled` has no effect on ntfy asks. Sessions running
as your user see each other's replies on the stream, but a reply carries only
its own token. A same-uid process can read your ntfy token from `.env`
anyway, which is out of scope as in [Broker spool](#broker-spool).
```

## Validation

- `rg -n "ntfy ask \(reply topic\)" docs/security-threat-model.md` finds the new
  section.
- The anchor links resolve when the Markdown is rendered.
- The user approves the text, which is the gate for phase 2.

## Risk and rollback

Docs only. If the design changes later, revert or edit the section.
