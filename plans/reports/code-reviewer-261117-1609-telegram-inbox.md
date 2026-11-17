# Code review: PR #14 `feat/telegram-inbox` (operator commands from Telegram)

Head reviewed: `9c137e1` (`git diff origin/main...origin/feat/telegram-inbox`, 26 files, +1808/-81).
Worktree: `/home/workspace-1/w/tvtdev94/mcgram-wt/telegram-inbox` (read-only; mutations were run in a `/tmp` copy).

## Verdict

**Nothing critical.** The operator filter, key handling, dedupe across poll-owner handover, read-once
semantics, and the disabled no-op path all hold up. Three medium findings: one addressing typo that
sends a command to every session, one way to silently suppress commands (`/stop` included), and one
test that passes for the wrong reason. The rest are low.

Checks run:
- `uv run pytest -q --timeout=30 tests/test_inbox.py tests/test_tools_check_inbox.py tests/test_multi_instance_stdio.py`: 59 passed
- `tests/test_ask_server.py` x5: 33 passed each time
- `ruff check src tests`: clean
- CI on PR #14: all 4 jobs pass now. The first py3.11/ubuntu run failed in `tests/test_ask_server.py::test_serves_a_request_and_writes_the_answer`, a file this PR does not touch, and passed on rerun (job 133937951504).

## Medium

### M1. `/stop @api` (with a space) goes to every session
- `src/mcgram/inbox.py:50-52`, `:128`, `:134`. The target is only parsed when `@label` touches the command. With a space, `@api` becomes args and `target=None`, which means broadcast.
- Verified: `parse_update("/stop @api")` returns `Command(command='stop', target=None, args='@api')`.
- What goes wrong: the operator means to stop one session and types a space (phone keyboards add one easily). Every session gets the command. `/note @web use the prod DB` reaches every project the same way. The addressing fails open, toward more recipients.
- Fix: when args start with `@<label>` as their own token, either use it as the target or refuse the command (`ignored`/`ambiguous_target`, audited). Add a test for each case.

### M2. Planted or junk files can silently drop real commands, `/stop` included; legitimate use can hit the cap too
- `src/mcgram/inbox.py:194-195` and `:210-212`. Both the cap and the dedupe count raw filenames. The HMAC is only checked on read (`:239`).
- `src/mcgram/inbox.py:313-315`. A `duplicate` is only logged at debug level and is never audited.
- `src/mcgram/spool.py:260-265`. Sweep keeps any file whose `expires_at` is in the future, however far.
- Verified in a temp dir:
  - planting `u101.cmd.json = {"expires_at": 1e12}` makes the real update 101 come back as `duplicate`, with no audit entry;
  - 50 such files make every later command `inbox_full`, and `sweep()` removes 0 of them.
- What goes wrong: the threat model's own adversary ("prompt injection with shell access writes `inbox/u1.cmd.json`") cannot forge a command, but it can mute the operator. One compromised session can keep `/stop` from reaching the other, healthy sessions. Update ids are easy to predict, because `audit.jsonl` (same uid) logs every `update_id`.
- Legitimate use can hit the same wall. `MAX_PENDING` counts records that were already read, and they stay until they expire. With `ttl_s: 86400`, 50 commands a day fill the inbox. After that `/stop` is dropped and the only signal is that no 👀 appears.
- Fix:
  - Count only authentic, unexpired records toward `MAX_PENDING`.
  - On `FileExistsError`, read the existing file. If it is not authentic or not for this `update_id`, audit it (for example `dropped/occupied`) and replace it.
  - Have sweep (or the inbox reader) delete `cmd` records whose `expires_at` is more than `now + 86400` (the largest `ttl_s`).
  - Give the operator a best-effort signal on `inbox_full`, `rate_limited` and `stale`, for example a ⚠️ reaction sent from a background task so the poll loop is never blocked.
  - Add this "suppression" case to the threat model.

### M3. Test passes for the wrong reason: nothing tests the expiry check
- `tests/test_tools_check_inbox.py:169-176`. It publishes with `sent_at = now - ttl - 10`, so the `since` check (`inbox.py:265`) already hides the record. The expiry check is never what decides the result.
- Verified by mutation: I deleted `and expires_at >= now` (`inbox.py:266`) in a copy. All 52 tests in `test_inbox.py` and `test_tools_check_inbox.py` still pass.
- What goes wrong: if the read-side expiry check regresses, a long-running session can re-read records that are expired but not yet swept (up to `sweep_interval_s`), and no test catches it.
- Fix: publish a record with `sent_at` after `started_at` and an `expires_at` in the past (call `publish` with an injected `now`, or call `_visible` directly).

## Low

### L1. A clock more than 60 s off hides new commands or shows old ones
- `src/mcgram/inbox.py:265` compares Telegram's `date` with the local `started_at`, allowing 60 s.
- Local clock 120 s fast: commands sent in the first ~60 s after a session starts are never shown to it.
- Local clock 120 s slow: a `/stop` sent up to ~180 s before the session started is shown. That breaks the README's promise that a new session does not obey an old `/stop`.
- This is plausible after sleep, or on VMs and WSL, where clocks drift.
- Fix: measure the offset from the HTTP `Date` header of the `getUpdates` response and store it in the record, or document that an NTP-synced clock is required.

### L2. Any `/command` is stored, not only `/status`, `/stop`, `/note`
- `src/mcgram/inbox.py:122-127`. The only reserved name is `/start`. Verified: `/deploy prod` and `/rm_rf` are stored and returned to Claude. `tests/test_tools_check_inbox.py:150` depends on `/x` being accepted.
- Plan decision 7 ("`/status`, `/stop`, `/note` only", in `plans/261117-0906-issue4-telegram-operator-commands/plan.md`) reads like an allowlist. The skill instead tells Claude to answer unknown commands.
- Fix: if decision 7 means an allowlist, add one at intake and audit everything else as `ignored/unknown_command`. Otherwise reword the decision. See the open question below.

### L3. Labels can collide, and some addresses fail silently
- `src/mcgram/inbox_wiring.py:65-77`.
- Two sessions in folders with the same basename, or two sessions in the same folder (a common setup), get the same label. `/stop@app` then stops both, and nothing warns about it.
- `/stop@MyBot` (the bot's own username, which Telegram's command autocomplete can insert) and `/stop@my-project` (a hyphen, so `not_command`) reach no session. Verified with `parse_update`.
- Fix: document both cases in the README section. Optionally, audit `target` values that no live session uses.

### L4. A lone surrogate in the text crashes intake with no audit entry
- `src/mcgram/inbox.py:151-152`. `_signature` calls `.encode("utf-8")`, which raises `UnicodeEncodeError` for a lone surrogate. `publish` (`:209`) does not catch it.
- Verified: `publish` raises on `/note a\ud800b`. The dispatcher logs the exception, the command is lost, and there is no `_inbox` audit row.
- Only the operator can send this, and normal clients should not produce it.
- Fix: strip category `Cs` in `sanitize`, or catch the error and audit `dropped/malformed`.

### L5. Turning the inbox off leaves command records on disk indefinitely
- When `inbox.enabled` is false, nobody sweeps `spool/<id>/inbox/`, and the records contain `args`.
- Only the plan's rollback section says to delete the directory. The threat model says records are "swept by every instance", which stops being true once the inbox is off.
- Fix: say this in the README and threat model, or have every instance keep sweeping the inbox namespace even when the inbox is off. That is cheap and does not register the tool.

## Verified non-issues (risk calibration)
- **Operator filter** (`inbox.py:103-121`). The update must pass `chat.id`, `chat.type == private`, `from.id`, `is_bot is False`, no `via_bot`, and no forward fields. Edited messages, captions, channel posts, `business_message` updates and callbacks never reach storage. `operator_chat_id <= 0` disables the inbox (`inbox_wiring.py:111`).
- **HMAC key.** It is derived in memory from the bot token (`inbox.py:143-146`) and never written to disk. Records are created `O_EXCL` with mode 0600 in a probed 0700 directory. The signature covers every field and is tied to the filename's `update_id`. A tampered, unsigned, other-bot or renamed record is rejected and audited once.
- **Replay after handover.** A new owner starts at `offset=0`. Dedupe uses `O_EXCL` per `update_id`, and `expires_at = max(now, sent_at) + ttl` together with the stale check holds whichever way the clock is skewed.
- **Read-once.** Nothing awaits between `_read` and `mark_seen` (`check_inbox.py:234-252`), so two concurrent calls in one session cannot both get the same command. `seen` is pruned only by expiry.
- **`inbox.enabled: false`.** `start_inbox` returns before it registers a handler or writes anything (`inbox_wiring.py:88-89`), the tool is not listed (`server.py:66-69`), and the e2e test asserts that the tool is absent.
- **`ask`.** It is unaffected: `AskRegistry.handle_update` only handles `callback_query`. The intake handler is synchronous file I/O only.
- **Test processes.** The e2e test closes both subprocesses in `finally`. The `World` fixture cancels its tasks and closes its clients. Both Windows CI jobs pass.

## Open questions
1. Does plan decision 7 mean an intake allowlist (L2), or only the skill's vocabulary?
2. Should a `/command` that is dropped (`inbox_full`, `rate_limited`, `stale`) give the operator a visible signal? The current design gives none (M2).
3. Local `uv run mypy src/mcgram` reports 37 errors, including some in code this PR does not touch (for example `spool.py:264`), while CI mypy passes. Probably a local environment difference; I did not check it against `main`.

Status: DONE_WITH_CONCERNS
Summary: Nothing critical. Three medium findings: `/stop @label` with a space goes to every session, planted or legitimate files can silently drop commands including `/stop`, and the read-side expiry check has no test (a mutation survives). Five low findings cover clock skew, the missing command allowlist, label collisions, an unhandled lone surrogate, and records left on disk after disabling.
