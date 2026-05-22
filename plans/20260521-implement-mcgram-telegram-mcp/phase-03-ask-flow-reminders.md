---
phase: 3
title: Ask Flow & Reminders
status: completed
priority: P2
effort: 1.5d
dependencies:
  - 2
---

# Phase 3: Ask Flow & Reminders

## Overview

Add the two interactive features: `ask` (post a question with inline keyboard, block until reply or timeout, accept both button taps and free-text replies) and in-session reminders (asyncio scheduler with cancel/list). After this phase Claude Code can wait for user input + schedule nudges.

## Context Links

- Brainstorm: [`../20260521-brainstorm-mcgram-telegram-mcp/brainstorm-report.md`](../20260521-brainstorm-mcgram-telegram-mcp/brainstorm-report.md) §4 (tools table), §5 (timeout cap)
- Telegram Inline Keyboard: https://core.telegram.org/bots/api#inlinekeyboardmarkup
- Telegram callback_query: https://core.telegram.org/bots/api#callbackquery

## Requirements

**Functional**
- Tool `ask(question: str, options: list[str] | None = None, timeout_s: int | None = None) -> {value: str, source: "button"|"freetext"|"timeout", question_id: str}`
  - `options` capped at `limits.ask_options_max` (default 6); if `None` → free-text only
  - `timeout_s` defaults to `defaults.ask_timeout_s`; capped at `limits.ask_timeout_max_s`
  - Post message with inline keyboard (one button per option, callback_data = `q_<id>:<index>`)
  - Block on `asyncio.Future` keyed by `question_id`
  - Resolve on: matching `callback_query`, OR `message` reply within the chat after question post (first wins)
  - On timeout: cancel future, return `{source: "timeout"}`, edit message to strip buttons + append `(timed out)`
  - On resolve: edit message to strip buttons + append `(answered: <value>)`
  - Always send `answerCallbackQuery` so Telegram client stops the spinner
- Tool `set_reminder(text: str, delay_s: int) -> {reminder_id, fires_at}`
  - `delay_s` capped at `limits.reminder_max_delay_s` (24h)
  - Total pending capped at `limits.reminder_max_pending`
  - Schedules `asyncio.create_task(_fire_after(delay_s))` → at fire time calls `send_message(f"⏰ {text}")`
- Tool `cancel_reminder(reminder_id: str) -> {ok: bool}` — cancels pending task
- Tool `list_reminders() -> [{id, text, fires_at}]`
- All reminder state in-process; lost on shutdown (documented)

**Non-functional**
- Concurrent `ask` calls supported (each has own question_id + future)
- No reminder drift > 1s for delays < 1h
- Modules < 200 LOC

## Architecture

```
src/mcgram/
├── ask_registry.py     # PendingAsk dataclass, dict[question_id, PendingAsk], handler
├── reminders.py        # ReminderScheduler with create/cancel/list, tied to event loop
├── tools/
│   ├── ask.py
│   ├── set_reminder.py
│   ├── cancel_reminder.py
│   └── list_reminders.py
└── runtime.py          # MODIFY: add ask_registry, reminder_scheduler
```

### Ask flow

```python
@dataclass
class PendingAsk:
    question_id: str        # "q_" + 8 hex
    future: asyncio.Future
    message_id: int         # so we can edit it later
    chat_id: int
    options: list[str] | None
    posted_at: float        # epoch — used to discriminate replies

class AskRegistry:
    def __init__(self): self._pending: dict[str, PendingAsk] = {}

    async def open(self, client, audit, ...) -> dict:
        q_id = "q_" + secrets.token_hex(4)
        kb = _build_keyboard(q_id, options) if options else None
        msg = await client.send_message(chat_id, question, reply_markup=kb)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[q_id] = PendingAsk(q_id, fut, msg["message_id"], chat_id, options, time.time())
        try:
            value, source = await asyncio.wait_for(fut, timeout=timeout_s)
            await client.edit_message_reply_markup(chat_id, msg["message_id"], reply_markup=None)
            return {"value": value, "source": source, "question_id": q_id}
        except asyncio.TimeoutError:
            await client.edit_message_text(chat_id, msg["message_id"], f"{question}\n\n_(timed out)_")
            return {"value": "", "source": "timeout", "question_id": q_id}
        finally:
            self._pending.pop(q_id, None)

    async def handle_update(self, update):
        if cq := update.get("callback_query"):
            data = cq["data"]                    # "q_abc:2"
            q_id, idx = data.split(":", 1)
            if pending := self._pending.get(q_id):
                value = pending.options[int(idx)]
                pending.future.set_result((value, "button"))
            await client.answer_callback_query(cq["id"])
        elif msg := update.get("message"):
            # Resolve OLDEST pending ask in same chat as freetext, only if posted before this msg
            for p in sorted(self._pending.values(), key=lambda p: p.posted_at):
                if p.chat_id == msg["chat"]["id"] and msg["date"] > p.posted_at:
                    p.future.set_result((msg["text"], "freetext"))
                    break
```

### Reminder scheduler

```python
class ReminderScheduler:
    def __init__(self, client, settings, audit):
        self._pending: dict[str, asyncio.Task] = {}
        self._meta: dict[str, dict] = {}    # id → {text, fires_at}

    def create(self, text, delay_s) -> dict:
        if len(self._pending) >= self._settings.limits.reminder_max_pending:
            raise LimitError("reminder_max_pending")
        if delay_s > self._settings.limits.reminder_max_delay_s:
            raise LimitError("reminder_max_delay_s")
        rid = "r_" + secrets.token_hex(4)
        fires_at = time.time() + delay_s
        self._meta[rid] = {"text": text, "fires_at": fires_at}
        self._pending[rid] = asyncio.create_task(self._fire(rid, delay_s, text))
        return {"reminder_id": rid, "fires_at": iso(fires_at)}

    async def _fire(self, rid, delay_s, text):
        try:
            await asyncio.sleep(delay_s)
            await self._client.send_message(self._settings.bot.operator_chat_id, f"⏰ {text}")
            self._audit.write({"tool": "_reminder_fire", "status": "ok", "rid": rid})
        finally:
            self._pending.pop(rid, None)
            self._meta.pop(rid, None)

    def cancel(self, rid) -> bool:
        if t := self._pending.pop(rid, None):
            t.cancel()
            self._meta.pop(rid, None)
            return True
        return False

    def list(self) -> list[dict]:
        return [{"id": rid, **m} for rid, m in self._meta.items()]
```

## Related Code Files

- **Create:**
  - `src/mcgram/ask_registry.py`
  - `src/mcgram/reminders.py`
  - `src/mcgram/tools/ask.py`
  - `src/mcgram/tools/set_reminder.py`
  - `src/mcgram/tools/cancel_reminder.py`
  - `src/mcgram/tools/list_reminders.py`
- **Modify:**
  - `src/mcgram/runtime.py` — wire `AskRegistry` + `ReminderScheduler` into `AppState`
  - `src/mcgram/server.py` — register 4 new tools; register `AskRegistry.handle_update` with `UpdateDispatcher`

## Implementation Steps

1. **ask_registry.py** — implement `PendingAsk` + `AskRegistry` per pseudocode. Build `inline_keyboard` as list of single-button rows.
2. **tools/ask.py** — input validate (options length, timeout cap). Call `registry.open(...)`. Audit log {tool: ask, status, source, ms_to_resolve}.
3. **reminders.py** — `ReminderScheduler`. Cancel ALL tasks on shutdown (called from server.py finally block).
4. **tools/set_reminder.py + cancel_reminder.py + list_reminders.py** — thin wrappers.
5. **server.py** — register 4 tools; on init: `dispatcher.register(ask_registry.handle_update)`; on shutdown: `reminder_scheduler.shutdown()`.
6. **Manual smoke test** —
   - `ask("Deploy now?", ["Yes","No"], 60)` → tap "Yes" → returns `{value:"Yes", source:"button"}`
   - `ask("Why?", None, 30)` → type "because" → returns `{value:"because", source:"freetext"}`
   - `ask("Wait", ["A","B"], 5)` + don't reply → returns `{source:"timeout"}` + edited message shows `(timed out)`
   - `set_reminder("test", 10)` → 10s later receives `⏰ test`
   - `set_reminder` × 11 → 11th rejected `reminder_max_pending`

## Success Criteria

- [ ] `ask` with options returns button value within timeout
- [ ] `ask` accepts freetext reply when posted after question
- [ ] `ask` timeout edits message to show `(timed out)` and returns `source: timeout`
- [ ] `ask` strips inline keyboard after resolve (no stale tappable buttons)
- [ ] `answerCallbackQuery` always sent (verify in Telegram client UX: button shows green tick, not spinner)
- [ ] Two concurrent `ask` calls resolved independently by `question_id`
- [ ] `set_reminder` fires within ±1s of expected time for delays ≤ 1h
- [ ] `cancel_reminder` stops pending fire; `list_reminders` excludes cancelled
- [ ] On MCP shutdown: all pending reminder tasks cancelled cleanly (no asyncio warnings)
- [ ] All new modules < 200 LOC

## Risk Assessment

- **Race: button tap arrives during edit_message_reply_markup**: idempotent — `answer_callback_query` always responds. Pending entry already removed → second tap silently ignored.
- **Freetext message racing button tap**: first-future-resolves-wins (asyncio.Future is single-set). Subsequent setter caught by `InvalidStateError` guard.
- **Reply matching too greedy**: free-text matches OLDEST pending ask in same chat. Document edge case: user must reply in question order. Acceptable simplification.
- **Reminder fires after shutdown started**: shutdown cancels all → `_fire` catches `CancelledError`, no send.
- **Markdown in `⏰ {text}` breaks parse_mode**: default to `plain` for reminder fire; let user opt-in.

## Security Considerations

- All updates already filtered by operator_chat_id at dispatcher layer (Phase 2)
- `question_id` is random hex — not guessable from outside (defence-in-depth only; operator already trusted)
- Reminder text length capped 1000 chars to prevent abuse

## Next Steps

Phase 4 — CLI (init/doctor/audit) + companion Claude skill.
