---
phase: 6
title: "Docs, examples, skill, CHANGELOG"
status: done
priority: P2
dependencies: [4, 5]
effort: 1.5h
---

# Phase 6: Docs, examples, skill, CHANGELOG

## Context

These are the spots that currently state "ask is Telegram-only":

- `README.md`: lines 24, 29, 71, 106, 111 and 300.
- `docs/architecture.md`: lines 75-79 (ntfy "one-way"), 137-146 (ask data
  flow) and 186.
- `src/mcgram/data/skill/SKILL.md`: lines 32, 39 and 41-42.
- The config examples are `config.example.yaml` and
  `src/mcgram/data/config.example.yaml:8-11`.

## Files (owned by this phase only)

`README.md`, `docs/architecture.md`, `docs/manual-smoke-test.md`, `CHANGELOG.md`,
`config.example.yaml`, `src/mcgram/data/config.example.yaml`,
`src/mcgram/data/skill/SKILL.md`.

## Steps

1. **CHANGELOG `[Unreleased]` → `### Added`.** The entry reads:
   "**`ask` over ntfy (opt-in).** With `ntfy.ask.enabled: true`, `ask` on an
   ntfy channel posts up to three action buttons. A tap posts to a reply topic
   that mcgram subscribes to while the question is open. It requires an access
   token, a question topic nobody can read anonymously, and a reply topic
   anyone can post to but only you can read. mcgram checks the two topics
   before every ask. There is no `multi_select` on ntfy. It works in every
   session without the broker. **Off by default**: read the ntfy section of
   `docs/security-threat-model.md` first."
2. **README.**
   - Tool table row (`:106`): ntfy becomes "✅ opt-in (≤3 options)".
   - Rewrite `:111` and `:300`.
   - Add a short "ask over ntfy" subsection with the setup for ntfy.sh (two
     reserved topics with their access levels) and for self-hosted
     (`auth-default-access: deny-all` plus the two `ntfy access` lines).
   - Note on the iOS limitations: `clear` is ignored, and an http tap opens the
     app ([ntfy#1728](https://github.com/binwiederhier/ntfy/issues/1728)).
3. **`docs/architecture.md`.**
   - Change the ntfy transport bullet to "one-way unless `ntfy.ask` is on".
   - Add "Data flow: an `ask` over ntfy". Reuse the plan's design diagram,
     covering the listener lifecycle and reconnect cursor.
   - Add `ntfy_ask.py`, `ntfy_reply_listener.py` and `tools/_ask_ntfy.py` to
     the module map (`:9-59`).
4. **Config examples.** Add a commented `ask:` block under `ntfy:`, matching
   the plan's config shape. Keep it disabled.
5. **SKILL.md.**
   - Transport table: `ask` on ntfy is "✅ if the user enabled ntfy ask (≤3
     options, no multi_select)".
   - Add guidance for `ntfy_topic_not_protected`, `ntfy_ask_requires_token`
     and `ntfy_reply_unreachable`: do not retry, tell the user what to fix.
   - `transport_unsupported` keeps its current guidance.
   - Decision 5: the stale "ask without options" row (`:25`) is fixed in a
     separate PR (#13). Do not change that line here.
6. **`docs/manual-smoke-test.md`.** Add a manual checklist. This is not run in
   CI and needs a self-hosted ntfy (Docker) plus a real phone. It is the only
   check of behaviour the mocks cannot prove:
   - the `open` event carries `time`;
   - the anonymous-read status code (401 or 403);
   - the phone POSTs the action body verbatim;
   - `sequence_id` replaces the notification;
   - the Android and iOS tap behaviour.

   This also addresses the existing `TODO(ntfy-auth-e2e)` at
   `src/mcgram/ntfy_client.py:32`. Phase 6 does not own that file, so removing
   the TODO is left to the phase that verifies it.

## Validation

- Check every link and `file:line` claim against the merged code
  (`rg -n "ntfy.ask" README.md docs src/mcgram/data`).
- `uv run pytest tests/test_skill_installer.py -q`, if the skill is
  snapshotted there.

## Risk and rollback

Docs only. Revert freely.
