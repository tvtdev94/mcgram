# Fix report — CI red, transport errors, Discord reminders

Date: 2026-09-21 20:42 (+07) · Branch: `main` · Not committed.

Scope approved by user: #1 CI red, #2 httpx error path, #3 test assertion,
#4 Discord reminders, #5 release doc. Publish stays manual (explicit decision).
Distribution work dropped — mcgram is personal-use.

## Outcome

| Before | After |
|---|---|
| CI red since 2026-07-27, 3 releases shipped red | suite green, 3 consecutive full runs |
| 351 passed / 1 failed (local), 350–351 + 1 failed (CI) | 361 passed, 1 skipped (POSIX-only) |
| ruff clean on `src/ tests/` only | ruff clean incl. `scripts/` |
| mypy 23 errors | 23 errors — unchanged, none added |

## Root causes found

**A. `ask` returned `internal` (CI ubuntu py3.12).** Raw `httpx` errors escaped
all three transport clients. `tg_client.py:54`, `ntfy_client.py:66`,
`discord_client.py:155` called httpx with no `except`; tools caught only
`TelegramError`/`NtfyError`/`DiscordError`, so a connect failure fell through to
`server.py:80-82` → `{"error": "internal"}` with **no audit line**. For a
notify-me-when-away tool this was the worst path: message lost, no record.

**B. Test assertion env-fragile (CI windows + local).**
`assert out["poll_owner_pid"] == owner.proc.pid` compared the server's real
`os.getpid()` against `Popen.pid`. uv's Windows venv `python.exe` is a
trampoline that re-execs the interpreter as a child — verified:

```
Popen pid : 4040
getpid()  : 8208
```

Reproduced 5/5 locally; CI windows failed identically (`assert 7940 == 7112`).
Not a flake, not the lock race.

**C. `_split_owner` inferred ownership from one probe.** Treated "not
`polling_not_owned`" as "is owner". Since the owner's probe failed with the
ambiguous `internal` from (A), a misread was possible.

**D. Discord reminders never fired.** `ReminderScheduler.__init__` took only
telegram + ntfy clients; `_require_client_for` skipped discord, so `create()`
returned a `reminder_id`; `_fire` then hit the telegram branch's
`assert dest.chat_id is not None` and died. Audited as `status: error`, but the
caller had already been told success. Zero test coverage.

## Changes

Source:
- `errors.py` — new `TransportError` + `from_httpx()` classmethod that scrubs
  secrets out of the message. httpx errors can embed the request URL, and both
  the Telegram base URL (`/bot<TOKEN>/`) and a Discord webhook URL carry a
  credential — that would have landed in `audit.jsonl`.
- `tg_client.py` / `ntfy_client.py` / `discord_client.py` — wrap the httpx call,
  raise `TransportError` with the transport's own secret scrubbed.
- `tools/send_message.py` / `send_file.py` / `send_video.py` / `ask.py` — catch
  `TransportError`, return `transport_unreachable` + `hint`, write audit line.
- `dispatch.py` — `_require_client` → `require_client` (public; reminders needs
  up-front validation without duplicating the transport→client map).
- `reminders.py` — `ReminderScheduler(state)` instead of carrying its own
  clients; `_fire` delegates to `dispatch.send_text`. Works for every transport,
  including any added later. Net −20 LOC.
- `server.py` `_wire_phase3` — pass `state`.
- `tools/set_reminder.py` — schema said "to a Telegram channel"; now names all
  three.

Tests:
- `test_multi_instance_stdio.py` — pid read from the lock file (authoritative,
  written by the lock holder); `_split_owner` probes both and asserts exactly one
  degraded; owner probe must be `transport_unreachable`, pinning regression (A).
- `test_transport_errors.py` (new, 6 tests) — connect failure and timeout →
  `transport_unreachable`; audit line written; bot token scrubbed; `from_httpx`
  scrubbing incl. `None`/empty secret; empty httpx message → no dangling colon.
- `test_tools_discord.py` +2 — reminder actually reaches the webhook and audits
  `ok`; webhook token never leaks on network failure.
- Constructor updates: `test_reminders.py` (new `_scheduler` helper),
  `test_tools_reminder.py`, `test_poll_ownership.py`, `scripts/live-smoke.py`.
- `scripts/live-smoke.py` — removed dead `rid` (pre-existing F841; CI lints only
  `src/ tests/` so it was never caught).

Docs:
- `docs/release.md` (new) — manual publish procedure, reconstructed from
  evidence; pre-publish checks; notes that tags do not publish.
- `CHANGELOG.md` — `[Unreleased]` section.
- `README.md` — link to the release doc from Develop.

## Verification

```
uv run ruff check src/ tests/ scripts/   → All checks passed!
uv run mypy src/                         → 23 errors (unchanged baseline)
uv run pytest -q  ×3                     → 361 passed, 1 skipped
```

## Not done (deliberate)

- Publish automation — user chose manual.
- Lock race `lock.py:86-96` (O_EXCL then write PID in a second step; a reader
  seeing the empty file treats a live lock as stale). Real but latent; no
  observed failure. Was misattributed as the cause of the test failure.
- 23 mypy errors, `py.typed`, sdist size, README badge drift, "<200 LOC" claim
  (false for 7 modules).
- Distribution work — dropped, personal use.

## Unresolved questions

1. Commit these changes? Nothing is committed; working tree dirty on `main`.
2. Ship as 0.5.1? If yes the CHANGELOG `[Unreleased]` section needs a version +
   date and the 4-place version bump per `docs/release.md`.
3. `transport_unreachable` is a new error code in tool results — the bundled
   skill (`src/mcgram/data/skill/SKILL.md`) was not reviewed for whether it
   should teach Claude how to react to it.
4. Was the ubuntu-py3.12-only nature of failure (A) timing or version specific?
   Fixed either way, but never root-caused as to why py3.11 passed.
