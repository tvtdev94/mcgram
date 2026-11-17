# ask over ntfy: implementation report

Plan: `plans/261117-0906-issue3-ask-over-ntfy/` (all six phases done).
Branch: `feat/ntfy-ask`, rebased on `origin/main` at 640625b (#13).

## What changed

- **Threat model** (`docs/security-threat-model.md`): the approved "ntfy ask
  (reply topic)" section, the diagram and adversary lines, and one "not
  defended" bullet. I added one "Required setup" bullet for the https decision.
- **Config** (`config.py`): `ntfy.ask {enabled, reply_topic}`, off by default.
  When it is on, the config refuses a missing or invalid reply topic (ntfy's
  own charset), a missing `access_token_env`, and a reply topic equal to any
  question topic.
- **Client** (`ntfy_client.py`): `has_token`, JSON `publish` (actions and
  sequence_id), `stream`, `poll`, and `anonymous_read_denied`. The probe never
  sends the token. Status mapping is now one shared `_raise_for_status`.
- **Core** (new `ntfy_ask.py` and `ntfy_reply_listener.py`): per-option 128-bit
  tokens, compared with `compare_digest`. The listener subscribes before the
  question is posted. Reconnects resume from a cursor taken from server values
  (the last message id, or the `time` of the `open` event). One catch-up read
  runs at the deadline. An unreachable reply topic returns
  `ntfy_reply_unreachable`, never `timeout`. A 401/403 fails every open ask
  with `ntfy_auth`. The outcome marker replaces the notification by
  `sequence_id` at priority 2. Shutdown is bounded to 5 s. `ask_registry.py`
  now shares its three marker strings, with Telegram output unchanged.
- **Tool wiring** (`tools/ask.py`, new `tools/_ask_ntfy.py`, `runtime.py`,
  `server.py`): the ntfy branch runs before the Telegram ownership refusals.
  The pre-flight refusals (`multi_select`, more than 3 options, a question over
  4096 bytes, no token, http) make no network call. The disabled path still
  returns `transport_unsupported`, now with an opt-in hint.
- **doctor** (`cli_doctor.py`): when `ntfy.ask` is on it checks the token, https,
  and anonymous read on every question topic and on the reply topic. With
  `--send` it also makes one anonymous publish to the reply topic.
- **Docs**: README (new "ask over ntfy" section plus the existing mentions),
  `docs/architecture.md` (an ntfy ask data flow and the module map),
  `docs/manual-smoke-test.md` (a manual checklist), CHANGELOG `[Unreleased] ->
  Added`, the bundled `config.example.yaml`, and SKILL.md (transport table and
  error guidance; line 25 untouched).

Size against `origin/main`: production about +834/-25 lines, tests about
+800/-8 lines (the new `tests/fake_ntfy.py` included).

## Test results

- `uv run ruff check src/ tests/`: all checks passed.
- `uv run pytest -q --timeout=30`: **674 passed** (626 on the base). Also run in
  full on Python 3.11 (674 passed); the local default is 3.14.
- `uv run mypy src/mcgram`: 37 errors in 15 files, the same error set as
  `origin/main` once line numbers are ignored. None is in a new file.
- `git diff --check origin/main`: clean.
- The new tests were written first and failed before the code existed. On top
  of that, I mutated each core guard once and checked that a test failed: size
  cap, question-id lookup, unreachable-vs-timeout, the deadline future surviving,
  subscribe-before-post, the reconnect cursor, the "connected" state after a
  drop, no retry on auth, and the registry not being built when disabled.
- Nothing was sent to a real ntfy, Telegram or Discord, and `~/.mcgram` was
  never read.

## Verified against ntfy sources (not from memory)

- Topic charset `^[-_A-Za-z0-9]{1,64}$`: `topicRegex` in `server/server.go`.
- An anonymous read of a denied topic answers 403: `authorizeTopic` returns
  `errHTTPForbidden` in `server/server_middleware.go`. Both 401 and 403 count
  as denied.
- `since=none` is a valid value in `parseSince`, and the `open` event carries
  `id` and `time` (`model.NewMessage`).
- JSON publish goes to the root and takes `sequence_id`; there are at most 3
  actions; a message over 4096 bytes becomes an attachment
  (docs.ntfy.sh/publish).
- Keepalive defaults to 45 s, under the Android app's 77 s;
  `NTFY_AUTH_DEFAULT_ACCESS` and `NTFY_AUTH_FILE` exist (docs.ntfy.sh/config).
- Updating notifications needs server v2.16.0 and Android v1.22.2
  (docs.ntfy.sh/releases).

## Not verified

- Real end-to-end behaviour: the phone POSTing the action body verbatim,
  `sequence_id` replacing the notification, and the tap behaviour on iOS and
  Android. That is what the new manual smoke-test section is for.
  `TODO(ntfy-auth-e2e)` in `ntfy_client.py` stays until that run happens.
- iOS support for notification updates (not on the releases page).
- Whether older ntfy servers accept `since=none`. If one does not, the probe
  gets a 400 and `ask` fails closed with `ntfy_api`.

## Deviations from the plan

These are also recorded in `plan.md` under "Implementation notes".

- The exceptions are named `*Error` (`NtfyTopicNotProtectedError`, and so on)
  to match the repo and ruff N818.
- The probe uses `since=none` instead of `since=latest`.
- If an `open` event ever lacks `time`, a reconnect or catch-up falls back to
  `since=all`.
- The root `config.example.yaml` is unchanged because it has no `ntfy:`
  section; only the bundled example got the `ask:` block.
- The iOS docs say that `clear` is ignored and the tapped notification stays in
  place. The plan's "an http tap opens the app" is not what ntfy#1728 says.
- The rejected-reply tests send each forged reply followed by a real tap and
  assert the real label. That is faster than waiting for a timeout and cannot
  pass vacuously.

## Incident: plan-store writes outside this worktree

`ak plan phase close mcgram/261117-0906 <n>` resolved phases 4 to 6 to the
`telegram-inbox` worktree's copy of this plan folder, which came from main.
It flipped `status: pending` to `done` in those three files there. I reverted
exactly those three lines (afterwards `git status` was clean for that path) and
set the statuses in this worktree by hand. The local plan store's index for
`mcgram/261117-0906` may still point at the sibling path. Plan `status:` is left
`in-progress` until the PR merges.

## Review follow-up (PR #15, review code-reviewer-261117-1609)

Each finding was re-checked against the code before any change was made.
M1 was reproduced: under anyio cancellation with a fake that suspends, only
the question was published and no stopped marker followed.

| Finding | Fix | Regression test (fails before the fix) |
|---|---|---|
| M1 ntfy | `_retire` and the listener stop run shielded with a 5 s deadline | `test_cancelled_call_retires_the_question_and_leaves_no_task` (anyio task-group cancel) |
| M1 Telegram | `_mark_abandoned` is shielded the same way, in its own commit | `test_ask_registry.py::test_cancelled_call_still_retires_the_question` |
| L1 | question published with `sequence_id = question id`; a lost response raises `NtfyPostUnconfirmedError`, which maps to "MAY have been posted" and retires the question | `test_failed_question_publish_says_whether_it_may_be_live` (the `never-connected` case is the control) |
| L2 | `_outcome()` shortens the quoted question to keep the message within 4096 bytes | `test_outcome_of_a_maximum_size_question_stays_within_ntfy_message_limit` |
| L3 | kept, as decided | none |
| L4 | doctor reads the reply topic with the token, without `--send` | doctor happy path (ask-on) and `token-cannot-read-reply-topic` |
| L5 | the catch-up read gets a 5 s budget; overhead documented | `test_hanging_catch_up_read_cannot_hold_the_ask_past_its_bound` |

The fake ntfy now suspends on every request, so cleanup that only worked when
every await completed synchronously can no longer pass by accident.

Gates after the follow-up: ruff clean; `uv run pytest -q --timeout=30` gives
**681 passed**; mypy reports 37 errors, the same set as `origin/main`;
`git diff --check` is clean. The branch was not rebased, as asked. No
`ak plan` command was run, and the plan files were edited by hand.
