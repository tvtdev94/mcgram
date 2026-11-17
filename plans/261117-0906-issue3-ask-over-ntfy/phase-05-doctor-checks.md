---
phase: 5
title: "doctor checks for ntfy ask"
status: done
priority: P2
dependencies: [2]
effort: 1.5h
---

# Phase 5: `mcgram doctor` checks

## Context

- `_check_ntfy` (`src/mcgram/cli_doctor.py:96-145`) checks ntfy health. It
  sends only with `--send`, and otherwise shows `[SKIP]` with `_NO_SEND`
  (`:32`).
- That send-only-with-`--send` rule came in through the Unreleased "Changed"
  entry and PR #10 (commit `946f6ac`).
- This phase can run in parallel with phases 3 and 4, because it touches
  different files and needs only phase 2's client methods.

## Files

- Modify: `src/mcgram/cli_doctor.py`
- Modify: `tests/test_cli_doctor_ntfy.py`

## Steps

Add these steps when `settings.ntfy.ask.enabled` is true, inside the existing
`async with NtfyClient(...)` block:

1. `ntfy ask token`:
   - `[FAIL]` when the env var is unset.
   - Otherwise `[OK]`, showing only the env var name, never the value.
2. `ntfy ask question topic protected` and `ntfy ask reply topic protected`:
   - Call `anonymous_read_denied`.
   - `[OK]` on True.
   - `[FAIL]` on False, with the fix for that topic. For the question topic:
     "reserve the topic / deny anonymous read". For the reply topic:
     "everyone write-only".
   - This is a read, so it runs without `--send`. It covers `default_topic` and
     every ntfy channel topic.
3. `ntfy ask reply topic accepts anonymous publish`:
   - Only with `--send`: publish anonymously to the reply topic with the body
     `{"probe":"mcgram doctor"}`. Running instances ignore it, because it has
     no `q`.
   - `[OK]` on 2xx. `[FAIL]` on 401/403, with the message "phone taps will be
     refused; set everyone write-only on the reply topic".
   - Without `--send`: `[SKIP]` with `_NO_SEND`.
4. Server URL `http://` → `[FAIL]`. Decision 1: ask requires https.

## Tests (load `test-audit` first)

Extend `tests/test_cli_doctor_ntfy.py` with these cases:

- **Enabled config, protected topics, no `--send`.**
  - Expect: probes only, and no POST to the reply topic.
  - Assert on the request list, extending
    `test_ntfy_only_happy_path_sends_nothing_by_default` (`:43`) in table form
    rather than duplicating it.
- **Topic publicly readable.** Expect a `[FAIL]` row and a non-zero exit.
- **`--send` and the anonymous publish.** The anonymous publish request carries
  no `Authorization` header, and a 403 gives a `[FAIL]` row.
- **Feature disabled.** Doctor output is unchanged; the existing tests cover
  this.

## Validation

`uv run pytest tests/test_cli_doctor_ntfy.py tests/test_cli_doctor.py -q`.

## Risk and rollback

Diagnostic only. Revert independently.
