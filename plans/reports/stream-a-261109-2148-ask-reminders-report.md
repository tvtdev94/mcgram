# Stream A — ask broker, durable reminders, richer ask

Branch `worktree-agent-a924aa43711d6aa5d`, fast-forwarded to `feat/v0.6.0`@f127960
(the worktree was created from `main`). The stream added five commits on top:

| Commit | What |
|---|---|
| 27d5240 | Non-polling sessions `ask` through the opt-in broker (broker plan phase 3) |
| 112caa9 | Durable reminders plus cross-session list and cancel (phase 4 and item 7) |
| d302096 | `ask` gains `multi_select`, `columns`, and a visible timeout (item 8) |
| 8d6a192 | Two-process test against a stdlib Telegram stub (phase 5 test) |
| e764eeb | Architecture, threat model, README and SKILL docs (phase 5 docs) |

Phase 2 (`ask_broker.py`, `ask_server.py` and their tests) was already complete at
f127960 and was kept as is. Its only change is carrying the new `ask` fields.

## Results

- `uv run pytest -q`: **527 passed, 7 skipped** (baseline 482 passed, 7 skipped). The 7 skips are POSIX-only spool checks on Windows.
- `uv run ruff check src/ tests/`: clean.
- Grep-check for stale doc claims (phase 5, step 10): 0 hits.
- `ask_registry.py` appears only in d302096 (item 8). It is untouched in the broker commits.
- Real `~/.mcgram/spool` checked after the runs: it does not exist, so no test pollution.
- Test file times: `test_reminder_spool` + `test_reminders` 3.6s; `test_ask_broker_e2e` ~3s; `test_multi_instance_stdio` 21s, of which the two new tests take about 2.5s each.

## Key decisions

1. **Broker off keeps the original refusal.** With no broker, `ask` still returns `polling_not_owned`, not phase 3's `broker_unavailable`. This satisfies the plan.md criteria "identical when off" and "spool unusable at boot → `polling_not_owned`". `broker_unavailable` is used only when the broker is on and a runtime spool write fails, which happens before anything is posted. With the broker on, `polling_not_owned` adds a `broker` reason field, for example "pid=N is no longer running".
2. **The owner supervisor is in `broker_wiring.serve_while_owner`, not `poll_ownership.py`.** It reads `state.ask_registry` on every tick, starts or stops an `AskServer` to match, and **withdraws its heartbeat on demotion**. Without that, requesters would block for up to 90s. A negative control confirmed the test fails when the withdrawal is disabled. The same task sweeps the asks spool while not owning.
3. **Reminder persistence is always on.** It uses the same spool (`reminders/` namespace) and does not depend on `broker.enabled`. If the spool probe fails, reminders stay in memory and a warning is logged.
4. **Orphan adoption** replaces phase 4's "reap dead-owner records". Records carry a pid (for liveness) and a per-process instance token (for ownership, so pid reuse cannot hide an orphan). Adopters claim with `write_exclusive` on the kind `claim<dead-owner-token>`, so exactly one wins each generation. To allow that, `spool._KIND_RE` was widened to `[a-z][a-z0-9]*`; it still rejects separators, dots and uppercase.
5. **Adoption rules.** Overdue reminders fire once at adoption with a "(late: was due …)" note. Orphans with a pending cancel are dropped. Adoption respects `reminder_max_pending` and the text/delay caps, and skips channels this instance cannot deliver to (configs may differ). Records expire 24h after their due time (`LATE_GRACE_S`) and are then swept unfired. Delivery is at-least-once (documented).
6. **`shutdown()` stays sync and keeps records; `start()` is sync.** The plan wanted async because of a cancel-mid-tick race. It can't happen here: every watch step is synchronous, so cancellation only lands on the sleep. Keeping them sync avoids touching `scripts/live-smoke.py` and four test call sites.
7. **`cancel_reminder` keeps `ok`.** A remote request returns `{"ok": true, "via": "broker", "note": …}`. Unknown, invalid or path-like ids return exactly `{"ok": false}`. The plan's `reason: unknown_reminder` would break the existing exact-equality tests the plan also says to keep.
8. **Richer ask.** `multi_select` values are always a list: ticked labels in option order on Done, `[text]` for a typed reply, `[]` on timeout. Timeout now edits to `⏱ timed out` and strips the keyboard, using the existing `tg_client` methods (`tg_client.py` was not modified). The new fields travel in the broker request; the owner validates them and the requester checks the value type.
9. **Test isolation.** The autouse `conftest` fixture redirects HOME/USERPROFILE, because the spool root is anchored to the home directory. Subprocess tests also set `broker.spool_dir`. Orphans in tests come from a real short-lived subprocess rather than a test-only pid/instance hook.

## New and changed modules (wc -l)

New: `ask_via_broker.py` 106, `broker_wiring.py` 134, `reminder_spool.py` 154, `tools/_ask_refusal.py` 70.
Changed: `tools/ask.py` 181, `reminders.py` 199, `server.py` 270, `ask_registry.py` 231 (item 8 allowed this), `ask_server.py` 222 (raw; under 200 code-only).

## Deviations from file ownership

- `spool.py`'s kind regex: owned, noted for review.
- `tests/conftest.py` and `tests/test_multi_instance_stdio.py`: test infrastructure for these features (the spool-isolation guard, `spool_dir` in config, the stub, `_is_degraded`). Not owned by Stream B.
- `poll_ownership.py`, `tg_client.py`, `cli*.py`: **not** touched.

## Left for integration (outside Stream A ownership)

- `mcgram doctor` spool row (`cli_doctor.py`) and the `broker:` comment in `data/config.example.yaml` (plan phase 5).
- CHANGELOG (Added: broker opt-in, durable reminders, `multi_select`/`columns`. Changed: `list_reminders` fields with the broker on, and the timeout marker `⏱ timed out`). Version bump.
- README "The 7 tools" heading and the stale "361 tests" count in the Architecture section (tool count changes with Stream B's `edit_message`).
- `server.py` conflict surface: this stream changed `_run` (reminder `start()`, broker task), `_wire_phase3` and the imports, but not the tool-module list.

## Unresolved questions

- None blocking. Consider adding an opt-out for reminder persistence if a user objects to reminder text on disk; it is documented in the threat model, and `redact_text` does not cover it.
