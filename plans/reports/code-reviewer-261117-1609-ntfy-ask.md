## Code Review Summary: `feat/ntfy-ask` (PR #15, head b047ef7)

### Scope
- Files: src/mcgram/{ntfy_ask,ntfy_reply_listener,ntfy_client,config,server,runtime,cli_doctor,ask_registry}.py, src/mcgram/tools/{ask,_ask_ntfy}.py, tests/{fake_ntfy,test_ntfy_ask,test_tools_ntfy,test_ntfy_client,test_config_ntfy,test_cli_doctor_ntfy}.py, docs/security-threat-model.md
- LOC: +2008 / -76 (31 files, about 700 of them src)
- Checks run in the worktree: `uv run pytest -q --timeout=30 tests/` gave 674 passed. `ruff check src tests` is clean. PR CI (ubuntu and windows, py3.11 and py3.12) is green per `gh pr checks 15`.

### Overall
I found no critical or security-blocking defects. The token, forgery, replay, subscribe-race and leak paths hold up against the code and against the ntfy server source. There is one verified medium defect: the question is not retired on the real MCP cancel and shutdown path, and the test that claims it is retired passes for the wrong reason. The other findings are low.

### Critical
None.

### Medium

**M1. A question is not retired when MCP cancels the call (Esc, or the session closing), so it stays on the phone with live buttons.**
- Code: `src/mcgram/ntfy_ask.py:129-131` (`except CancelledError: await self._retire(...)`) and `:190-196` (`wait_for` plus `except Exception`).
- Why it happens: mcp 1.27.1 cancels in-flight handlers through an anyio cancel scope. On transport close it calls `tg.cancel_scope.cancel()` (`mcp/server/lowlevel/server.py:686-690`). A user cancel goes through `RequestResponder._cancel_scope.cancel()` (`mcp/shared/session.py:145`). Anyio cancellation is level-triggered: every later await inside the handler is cancelled again. So the retire `publish` raises `CancelledError`, which is a BaseException and gets past `except Exception`. The publish is abandoned.
- By the time `server.py:138` calls `ntfy_ask.shutdown()`, the handler's `finally` has already popped `_pending`, so shutdown has nothing left to retire.
- Reproduced: I put `NtfyAsk.open` in an anyio task group with a FakeNtfy whose handler does `await asyncio.sleep(0.01)` (a real round trip suspends), then cancelled the scope. `fake.published` contained only the question, with no marker.
- Why the test passes for the wrong reason: `tests/test_ntfy_ask.py:273` passes only because it cancels with plain `asyncio` `task.cancel()` and because the pytest-httpx mock never suspends. The same mock-based test with no delay also "retires".
- Impact: the operator sees a live question after the agent has given up, and a tap is silently lost. This is UX and correctness, not security.
- Same pre-existing pattern on Telegram: `ask_registry.py:149-151` calls `_mark_abandoned`, which uses `wait_for` and `suppress(Exception)`.
- Fix (verified in a standalone repro):
  ```python
  with anyio.CancelScope(shield=True), anyio.move_on_after(_RETIRE_TIMEOUT_S):
      await self._client.publish(...)
  ```
  Use this in `_retire` instead of `asyncio.wait_for`, and apply the same change to `_mark_abandoned`. Change the shutdown test so it cancels via `anyio.create_task_group().cancel_scope.cancel()` against a fake that suspends.

### Low

**L1. A publish timeout is reported as "question was NOT posted" even when it may have been posted.**
- Code: `src/mcgram/tools/_ask_ntfy.py:120-123`, and `ntfy_ask.py:124-127` where `posted_id` is still None.
- `TransportError.from_httpx` wraps a `ReadTimeout` on the publish POST. That timeout can happen after ntfy stored and fanned out the question. The listener is then stopped in `finally`, so the phone shows working buttons that nobody hears, and the tool tells Claude the question was not posted.
- Fix: for `httpx.ReadTimeout` or `RemoteProtocolError` on the publish, report "question MAY have been posted". Telegram has a similar ambiguity, so match whatever wording it uses.

**L2. The outcome marker can go over ntfy's 4096-byte message limit.**
- Code: `_ask_ntfy.py:27,59` and `ntfy_ask.py:141,131`.
- The question is capped at 4096 bytes. The marker is the question plus a suffix of about 40 bytes (`"\n\n⏹ mcgram stopped before an answer"`). By the module's own comment at `_ask_ntfy.py:26`, ntfy turns anything past that size into an attachment. A question near the cap therefore gets a marker that becomes an attachment or is rejected. A rejection is swallowed at `:195`, and the original question with live buttons stays.
- Fix: cap the question at `4096 - 64` bytes, or truncate the question inside the marker text.

**L3. With `ntfy.ask.enabled: false`, the response is not byte-identical to before.**
- Code: `src/mcgram/tools/ask.py:128-131`.
- The `error`, `reason` and `channel` fields are unchanged, the audit record is unchanged, no network call is made (`state.ntfy_ask is None` per `server.py:229-237`), and configs without an `ask` key still load. Only the `hint` string gains "; ntfy ask is opt-in...".
- This is acceptable if intended. Just note that the requirement "identical to before" does not hold literally.

**L4. `doctor` does not check that the token can subscribe to the reply topic.**
- Code: `cli_doctor.py:158-214`.
- The only checks are anonymous-read denial and anonymous publish. A token without read access on the reply topic passes `doctor`. At runtime the first ask then fails with `ntfy_auth`. That failure is clean (nothing posted) but late.
- Fix: run an authenticated `poll(reply_topic, since="none")` and expect 2xx.

**L5. An ask can block for much longer than `ask_timeout_max_s`.**
- Code: `ntfy_ask.py:108-128,172`.
- The total is two probes, plus up to 10 s of `wait_ready`, plus the publish, plus `timeout_s`, plus the catch-up `poll`, which uses the default timeout of up to 30 s and has no deadline of its own, plus a retire of up to 5 s.
- Fix: bound the catch-up with a short `asyncio.timeout`, or subtract the setup time from `timeout_s`.

### Checked and found sound (with evidence)

**Answer tokens**
- `secrets.token_urlsafe(16)` gives 128 bits, fresh for every option of every question (`ntfy_ask.py:113`).
- The comparison is `secrets.compare_digest` on bytes (`:214-216`). A length mismatch just returns False.
- A question id is only an index into `_pending`. Authentication rests on the token, so a 32-bit `q_` id collision is harmless.

**Access token leakage**
- The `httpx.AsyncClient` has no default headers (`ntfy_client.py:55`). `anonymous_read_denied` passes no headers (`:276-277`).
- The action buttons carry only `{q, t}` (`ntfy_ask.py:158-161`). The test asserts that TOKEN is absent from the question and from the audit log.
- Transport errors are scrubbed via `from_httpx(..., self._token)`.
- The reply body is never logged or audited (`:217-220`).
- The fake routes unauthenticated `poll=1` to the probe, so a token sent on the probe would make the tests fail. The probe behaviour is therefore genuinely tested.

**Forged and replayed answers**
- An answer for an unknown or finished id is dropped (`:211-213`).
- A replayed cached reply from an earlier question matches no open id (test at `test_ntfy_ask.py:131`).
- Another session's ids are ignored and not audited (`:146`).

**Subscribe versus publish race**
- In ntfy `server.go` `handleSubscribeHTTP` (fetched from GitHub main), the order is `t.Subscribe`, then `NewOpenMessage`, then `sendOldMessages`.
- mcgram publishes only after `open` sets `ready` (`ntfy_reply_listener.py:57-63`, `ntfy_ask.py:120-126`).

**Reconnection cursor**
- In ntfy, `parseSince` accepts a message id (via `ValidMessageID`) or unix seconds. With no `since`, a stream replays nothing.
- On reconnect the cursor is the last id or the open `time`. Duplicate deliveries hit the `future.done()` guard, so there is no double resolution.
- The `sequence_id` JSON field, and reusing the returned `id` as the sequence id, are confirmed in docs.ntfy.sh/publish.

**Resource lifetime**
- The stream generator is closed via `aclosing`.
- `stop()` detaches the task and calls `task.cancel()` synchronously before its first await, so the listener task is cancelled even when the caller is itself being cancelled.
- The httpx client is shared and owned by `_build_clients`.

**Telegram `ask` and the broker**
- The ntfy branch runs only for `dest.transport == "ntfy"` with `ntfy_ask` set (`ask.py:123`).
- The `ask_registry.py` change only moves strings into helpers. The texts are unchanged.

**Config validation**
- The reply topic must match ntfy's topic regex, needs `access_token_env`, and must differ from `default_topic` and every ntfy channel topic.
- https and the token are enforced at runtime before any network call.

**Windows**
- There are no real sockets, path issues, or tight timer assertions (the smallest timeout is 0.3 s and the auth test bound is under 10 s). Windows CI is green. I found no Windows-specific risk.

### Recommended actions
1. M1: shield the retire with an anyio `CancelScope(shield=True)` plus `move_on_after` (ntfy and Telegram), and make the shutdown test use anyio cancellation with a fake that suspends.
2. L1: reword the "NOT posted" hint for ambiguous publish timeouts.
3. L2: add headroom to the question byte cap.
4. L4 and L5 are optional hardening.

### Metrics
- Tests: 674 passed, 44 s.
- Lint: 0.
- Type check: `mypy src` reports pre-existing errors only. None are on new lines; for example the `config.py:239-242` errors are the existing `default_factory` fields.
- Coverage: not measured.

### Unresolved questions
- Was the extra text on the disabled-path `hint` (L3) intended to count as "identical behavior"?

Status: DONE_WITH_CONCERNS
Summary: Nothing critical. The tokens, leak paths, forgery, replay, subscribe ordering and cursor handling are correct, and I checked them against the code and the ntfy server source. One verified medium issue: under MCP's anyio cancellation the question is never retired and stays live on the phone, and the shutdown test hides this because the mock never suspends.
