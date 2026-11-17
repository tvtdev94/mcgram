---
phase: 2
title: "Config and ntfy client primitives"
status: done
priority: P1
dependencies: [1]
effort: 3h
---

# Phase 2: Config and ntfy client primitives

## Context

- `NtfyConfig` is at `src/mcgram/config.py:42-52`: server, default_topic,
  access_token_env. The token is resolved at `config.py:384-387`.
- `NtfyClient` is at `src/mcgram/ntfy_client.py:21-196`. `_request` (`:56-79`)
  maps 401/403 to `AuthError` and other 4xx/5xx to `NtfyError`. It wraps httpx
  failures in `TransportError.from_httpx(..., token)` (`:70`), which scrubs the
  token. The shared client timeout is 30 s (`:18`), too short for a stream.
- The client is constructed once per process at `src/mcgram/server.py:208-212`.

## Requirements

### Config (`config.py`)

1. Add `class NtfyAskConfig(BaseModel)` with `enabled: bool = False` and
   `reply_topic: str | None = None`, and nest it in `NtfyConfig` as
   `ask: NtfyAskConfig = Field(default_factory=NtfyAskConfig)`.
2. Add a `Settings` model validator that applies only when `ntfy.ask.enabled`.
   It raises when:
   - `reply_topic` is missing or does not match `^[-_A-Za-z0-9]{1,64}$`. This
     charset is believed to be ntfy's, but **not verified**; confirm it against
     the ntfy server source during implementation.
   - `access_token_env` is unset. The message should say the feature needs a
     token and read-protected topics, and link to the threat model.
   - `reply_topic` equals `default_topic` or any ntfy channel's `ntfy_topic`.
     The reply topic is anonymously writable, so posting questions to it would
     let anyone inject fake questions.
3. When the feature is disabled, validation does not change at all.

### Client (`ntfy_client.py`)

Add these methods. Do not change the existing ones.

- `has_token` property: `bool(self._token)`.
- `publish(topic, message, *, title=None, priority=None, actions=None,
  sequence_id=None) -> dict`: a JSON POST to the server root, which is where
  ntfy requires JSON publishes to go
  ([publish docs, "Publish as JSON"](https://docs.ntfy.sh/publish/)). It goes
  through `_request`, so error mapping is shared. Omit `None` fields.
- `stream(topic, *, since: str | None) -> AsyncIterator[dict]`:
  - Request: `GET {base}/{topic}/json[?since=…]` via `self._client.stream`,
    with the auth header and a per-request
    `httpx.Timeout(10.0, read=90.0)`. The read timeout must exceed the 45 s
    keepalive default, which ntfy keeps under the Android app's 77 s limit
    ([config docs](https://docs.ntfy.sh/config/)).
  - Errors: 401/403 raises `AuthError`, any other status ≥ 400 raises
    `NtfyError`, and httpx errors raise a scrubbed `TransportError`.
  - Output: yields one parsed object per non-empty line and skips lines that
    are not JSON. Lines over 64 KiB are dropped. That is a sanity cap; the
    reply body has its own 1 KiB cap in phase 3.
- `poll(topic, *, since: str) -> list[dict]`: `GET …/json?poll=1&since=…`, then
  the same per-line parsing, so the parser is shared with `stream`. Replay is
  capped server-side at 10 MB per topic
  ([subscribe API](https://docs.ntfy.sh/subscribe/api/)).
- `anonymous_read_denied(topic) -> bool`:
  - Request: `GET {base}/{topic}/json?poll=1&since=latest` with **no** auth
    header. The shared `httpx.AsyncClient` must not carry default auth headers;
    today it does not, since auth is added per request at `:66`.
  - Result: True on 401/403, False on 200. Anything else raises
    `NtfyError`/`TransportError`.
  - Whether ntfy answers 401 or 403 for an anonymous read is **not verified**,
    so both count as denied.

## Tests (pytest-httpx; the installed version, 0.36.2, provides `IteratorStream`)

These go in `tests/test_ntfy_client.py`, extending the existing file and its
`_ntfy_ok` helper.

- `publish` sends JSON to the server root with `topic`, `actions` and
  `sequence_id`, plus the Bearer header. This guards the wire contract.
- `stream`:
  - It yields `open`, `message` and `keepalive` objects from an
    `IteratorStream` of NDJSON lines.
  - It skips blank lines and lines that are not JSON.
  - A 403 raises `AuthError`.
- `anonymous_read_denied`:
  - Table-driven over 401→True, 403→True, 200→False, and 500 raising.
  - It asserts that **no** `Authorization` header was sent. This is a security
    contract: a probe that sends the token would always look protected.
- `TransportError` from `stream` does not contain the token. Reuse the pattern
  of the existing scrub test, if there is one; check
  `rg -n "\*\*\*" tests/test_ntfy_client.py`.

These go in `tests/test_config_ntfy.py`, table-driven over the YAML snippets:

- The feature is off by default, and existing configs load unchanged.
- Enabling it raises a `ConfigError` naming the cause in each of these cases:
  no `reply_topic`, a bad charset, no `access_token_env`, and `reply_topic`
  equal to `default_topic` or to a channel topic.

## Validation

`uv run pytest tests/test_ntfy_client.py tests/test_config_ntfy.py -q`, then
`uv run ruff check src tests` and `uv run mypy src`.

## Risk and rollback

The change is additive: no caller exists until phase 4. Roll back with
`git revert`.
