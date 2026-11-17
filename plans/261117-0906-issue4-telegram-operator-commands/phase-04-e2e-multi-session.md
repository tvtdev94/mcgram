---
phase: 4
title: "Cross-process e2e"
status: completed
priority: P2
dependencies: [3]
effort: 2h
---

# Phase 4: Cross-process e2e (two real mcgram processes)

## Context

The in-process tests in phase 3 share one Python process. They cannot prove that the feature works
across separate processes:

- the owner and the non-owner hold separate `seen` sets and separate clocks;
- the label comes from each process's own env or cwd;
- the spool is shared across real processes;
- the tool list is fixed at `tools/list`.

`tests/test_multi_instance_stdio.py` already runs real processes over JSON-RPC against a
local Telegram stub:

- `_TelegramStub` is defined at `:61-113`, and tests inject updates with `push()`;
- unknown methods return `True` (`:106`);
- `_write_config` is at `:41`;
- `tool_names()` is at `:187-188`;
- the existing default-config check is at `:270-271`.

## Files

| Action | File |
|---|---|
| Modify | `tests/test_multi_instance_stdio.py` (the only file in this phase) |

## Steps

1. In `_TelegramStub.handle`, record every method name and form in `self.calls`, so the test can
   assert on the `setMessageReaction` request. Keep the existing behavior.
2. Give `_write_config` an `inbox: bool = False` parameter that emits `inbox: {enabled: true}`.
3. Add one test. It starts owner A with label `alpha` and non-owner B with label `beta`, using
   `MCGRAM_SESSION_LABEL` in each process's env. Then:
   1. Wait until A owns polling and the inbox heartbeat file exists.
   2. Push a private message from the operator: `{"message": {"message_id": 9, "date": now,
      "chat": {"id": OP, "type": "private"}, "from": {"id": OP, "is_bot": false},
      "text": "/stop@beta now"}}`.
   3. On B, call `check_inbox(wait_s=10)`. It returns `command == "stop"`, `args == "now"`, and
      `label == "beta"`.
   4. On A, call `check_inbox()`. It returns no commands, because the command is addressed to
      `beta`.
   5. The stub received `setMessageReaction` with `message_id == 9`.
   6. `"check_inbox"` is in `tool_names()` for both A and B.
4. In the existing test at `:270-271`, which uses the default config, add
   `assert "check_inbox" not in second.tool_names()`. That one line proves the off-by-default
   contract at the real `tools/list` boundary.

## Validation

`uv run pytest tests/test_multi_instance_stdio.py`. The module already sets `timeout(120)` (`:35`).

## Risk

| Risk | L×I | Mitigation |
|---|---|---|
| Flaky timing between the two processes | M×M | Wait on observable state (the heartbeat file, then the `wait_s` long read), never on a fixed sleep |
| The stub diverges from the real Bot API for `setMessageReaction` | L×L | The reaction is best effort. The manual smoke step (phase 5) checks it once against real Telegram |

## Rollback

Revert the test changes.
