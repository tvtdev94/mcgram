# Operator commands from Telegram (`check_inbox`): implementation report

Branch `feat/telegram-inbox`, pushed to origin. The PR is not opened and nothing is merged. Plan:
`plans/261117-0906-issue4-telegram-operator-commands/`. All five phases are marked completed,
and the user's eight decisions are recorded in `plan.md`.

## What changed

- **Threat model** (`docs/security-threat-model.md`): a new "Operator inbox" section. The trust
  boundary, adversary list, group row, and "does NOT defend" list now cover the inbox. This was
  committed before any code.
- **Config**: `inbox.enabled` (default `false`) and `inbox.ttl_s` (default 1800, range 60–86400).
- **`spool.heartbeat_status`**: the heartbeat checks moved out of `AskBroker.owner_alive`. The
  broker's reason strings are unchanged, and the broker tests pass unmodified.
- **`inbox.py`**: parses and filters updates, removes control and invisible characters, signs
  records with HMAC-SHA256, and holds `InboxStore` (publish, unread, heartbeat) and the intake
  handler.
  - Filters: private chat only, sender must be the operator, no bot senders, no `via_bot`, no
    forwards, no edits, text only, `/start` ignored, args limited to 1000 characters.
  - Dedupe uses `O_EXCL` per `update_id`. Updates older than `ttl_s` are dropped, at most 50
    records can be pending, and intake has its own rate bucket.
- **`inbox_wiring.py`**: boot checks set `unavailable` for:
  - no bot configured;
  - `bot.disable_polling`;
  - `operator_chat_id <= 0`;
  - no token;
  - an unsafe spool.

  It also works out the session label (`MCGRAM_SESSION_LABEL`, else the working folder name) and
  runs a supervisor. The supervisor beats while this session owns polling, withdraws only its own
  heartbeat, and sweeps in every session.
- **`tools/check_inbox.py`** and **`TelegramClient.set_message_reaction`**:
  - `check_inbox(wait_s)` waits at most `limits.ask_timeout_max_s`.
  - It returns at once with `receiving: false` when no session is polling.
  - It returns at most 20 commands per call and sets `more` when there are others.
  - Each command is returned once per session.
  - A record with a bad signature is audited once and never returned.
  - A best-effort 👀 reaction goes on each command that is read.
  - The tool is listed only when `inbox.enabled` is set.
- **Docs**: SKILL.md (new section, tool row, error recovery, rules for what Claude must not do,
  trigger phrases; the "ask without options" row is untouched), README (9 tools, an operator
  commands section, the config block, the limitation line), architecture, example config,
  CHANGELOG `[Unreleased] ### Added`, and the manual smoke test.
- `ask` is untouched. No version bump.

## Verification, first round (HEAD `3730a1d`)

| Gate | Result |
|---|---|
| `uv run ruff check src/ tests/` | All checks passed |
| `uv run mypy src/mcgram` | 37 errors in 15 files, same as origin/main. None in new files; per-file counts unchanged |
| `uv run pytest -q --timeout=30` | 679 passed (baseline 626; 53 new) |
| `git diff --check` | clean |

- New tests: `tests/test_inbox.py` (filter table on the real `poll_loop`, dedupe, stale, cap, rate
  limit, supervisor, label), `tests/test_tools_check_inbox.py` (tool boundary across sessions,
  pytest-httpx), and one two-process e2e in `tests/test_multi_instance_stdio.py`.
- Each guard was checked by mutation: breaking the guard temporarily made its test fail. The new
  modules did not exist when the first tests were written, so those failed at import. The
  regression tests added after review failed on the code before the fix.
- Started subprocesses are closed in `finally`. No stray `mcgram` process was left. Nothing was
  sent to real Telegram, ntfy or Discord.

## Review

A code-reviewer subagent found no critical issues. Fixed:

- a failed audit write could lose a command (it was marked read first);
- a failed folder listing could replay read commands (the read set was pruned to the keys on
  disk);
- clock skew could reopen dedupe;
- a deleted working folder crashed startup;
- the README wording about the 60 s allowance;
- untested sanitize, malformed and expiry paths.

Not changed, as the approved plan specifies:

- the intake rate token is taken before the duplicate and stale checks;
- the heartbeat follows lock ownership, not `getUpdates` health (now documented in
  `docs/architecture.md`).

Not fixed: a narrow race in which a demoted session could remove a new owner's heartbeat while
still running. It costs at most about 10 s of `receiving: false`.

## Second round: PR #14 review fixes

The independent review is in `plans/reports/code-reviewer-261117-1609-telegram-inbox.md` in the
main checkout. I reproduced every finding against the code before fixing it, and a scratch script
confirmed M1, M2, L2, L3 and L4. M3 was confirmed by mutation: with the read-side expiry check
deleted, all 52 inbox tests still passed. L5 was confirmed by reading `start_inbox`.

The fixes follow the controller's decisions.

| Finding | Fix | Test that fails when the fix is removed |
|---|---|---|
| M1 `/stop @api` broadcasts | `dropped/ambiguous_target` + 👎 | `test_intake_filter[spaced-target]` |
| L2 any `/command` stored | allowlist, `dropped/unknown_command` + 👎; skill updated | `test_intake_filter[unknown-command]` |
| M2 planted file takes a command's slot | the existing file is verified; anything not genuine is replaced (`replaced: bad_signature`) | `test_a_forged_file_cannot_occupy_a_command_slot`, `test_a_forged_file_replaced_by_the_real_command_is_read` |
| M2 junk and expired records fill the cap | only signed, unexpired records count toward it | `test_only_live_signed_records_count_toward_the_cap` |
| M2 records that claim to live for years | swept past `now + 86400 + 60` | `test_expired_records_are_swept_by_every_instance[beyond-max-ttl]` |
| M2 silent drops | 👎 on every `dropped` outcome, sent from a background task with its own bucket; duplicates audited `ignored/duplicate` | filter table, stale, cap, rate and redelivery tests |
| M3 expiry check untested | the test now runs a session older than `ttl_s` with a command sent after it started | `test_expired_records_are_hidden_before_the_sweep` |
| L4 lone surrogate crashes intake | `Cs` stripped in `sanitize` | `test_intake_filter[accepted]` |
| L5 records left when the inbox is off | one sweep at startup of an existing `inbox/` dir; it creates nothing | `test_disabling_the_inbox_still_sweeps_old_records_at_startup` |
| L1, L3 | README and threat model: NTP-synced clock; label clashes; `/stop@BotName` and hyphenated labels | docs only |

Notes:

- **The drop reaction is 👎, not ⚠️.** I checked the live Bot API page, and ReactionTypeEmoji does
  not include ⚠. The coordinator confirmed 👎.
- **`too_long` and `write_failed` get 👎 as well,** because the rule is every `dropped` outcome. The
  controller's list did not name these two.
- **Readers track forged files apart from read commands,** keyed by a fingerprint of the file's
  content. Otherwise the genuine command that intake writes over a forged file would stay hidden
  from sessions that already audited the forgery.
- **The sweep threshold adds 60 s of clock allowance to 86400.** A legitimate record expires
  `ttl_s` after the later of `received_at` and `sent_at`, so Telegram's clock being slightly ahead
  could otherwise push it past exactly `now + 86400`.
- **Remaining risk.** A same-uid process can still delete records or change the directory's mode.
  This is stated in the threat model.
- **Fixed an incorrect doc pointer.** I had pointed the README at `mcgram audit --rejected` for
  drops, but that command lists only `status == "rejected"` (`cli_audit.py:106`). The README now
  says `mcgram audit --tool _inbox`.

Gates on `796ce9b`:

- ruff: clean.
- mypy: 37 errors, same as origin/main.
- `uv run pytest -q --timeout=30`: **686 passed**, up from 679 after the first round and 626 on
  origin/main.
- `git diff --check`: clean.
- No stray processes.

## Deviations from the plan

These are listed in `plan.md` under "Implementation notes and deviations". The main ones:

- an extra `rejected/malformed` refusal;
- an audited `dropped/write_failed` outcome;
- a record moved under another update id is rejected as `bad_signature`;
- non-integer `wait_s` is rejected;
- read receipts are sent concurrently;
- the read cursor is pruned by expiry.

The plan's line references did not match origin/main, so the code follows the actual modules.

## Not verified

- Windows CI. Two new tests are skipped on Windows (removing the working folder, and directory
  permissions).
- `setMessageReaction` with 👀 against the live Bot API.
- The working folder Claude Code gives a stdio server, which is the default label. Both are in the
  manual smoke test.

## Needs attention (not mine, left untouched)

- `plans/261117-0906-issue3-ask-over-ntfy/phase-04-*.md` and `phase-05-*.md` in this worktree were
  changed (`status: pending` to `done`) at 16:01–16:02 by another process. This is most likely the
  sibling ntfy agent's `ak plan phase close`, because the `ak` plan-store row for that plan points
  at this worktree's copy.
- I did not commit or revert those files. The sibling's status updates belong in its own worktree.
- `ak plan phase close` failed with "plan not found" for this plan, so the phase status was set in
  the files directly, following the repo's `completed` convention. `ak plan status` shows 5/5 done.
  I did not run `ak plan reindex --apply`, because it would also rewrite the sibling plan's store
  row.
