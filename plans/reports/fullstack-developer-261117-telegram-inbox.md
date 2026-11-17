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

## Verification (run in the worktree, HEAD `3730a1d`)

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
