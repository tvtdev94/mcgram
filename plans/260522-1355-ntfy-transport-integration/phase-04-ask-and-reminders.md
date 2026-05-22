# Phase 04 — ask (graceful reject) + reminders (auto-work)

**Priority:** P1
**Status:** PENDING
**Depends on:** phase-03

## Overview

- `ask` cần 2-chiều → ntfy không hỗ trợ → **trả lỗi rõ ràng, không crash**.
- `set_reminder` / `cancel_reminder` / `list_reminders`: nội bộ gọi `send_message` → **tự động hoạt động trên ntfy** sau phase 03. Chỉ cần ReminderScheduler dùng đúng dispatch helper.

## `ask` behavior

```python
async def handle(state, *, question, options=None, timeout_s=None, channel=None):
    # ... validate input ...
    try:
        dest = state.settings.resolve_destination(channel)
    except ConfigError as e:
        return {"error": "invalid_input", "reason": "unknown_channel", "detail": str(e)}
    if dest.transport != "telegram":
        state.audit.write({
            "tool": TOOL_NAME, "status": "rejected",
            "reason": "transport_unsupported",
            "channel": dest.name, "transport": dest.transport,
        })
        return {
            "error": "transport_unsupported",
            "reason": "ask requires a telegram channel (ntfy has no 2-way input)",
            "channel": dest.name,
            "hint": "switch to a telegram channel or use send_message + set_reminder",
        }
    # ... existing telegram flow (chat_id = dest.chat_id) ...
```

## Reminders

`ReminderScheduler._fire` hiện gọi `self._client.send_message(chat_id, ...)`. Cần đổi sang **dispatch helper** để chọn transport theo channel của reminder.

Đề xuất: 1 hàm utility ở `runtime.py` hoặc `tools/_dispatch.py`:

```python
async def send_message_to(state: AppState, dest: Destination, text: str, *, silent=False) -> dict[str, Any]:
    if dest.transport == "ntfy":
        return await state.ntfy_client.send_message(dest.ntfy_topic, text, silent=silent)
    return await state.tg_client.send_message(dest.chat_id, text, disable_notification=silent)
```

ReminderScheduler nhận `AppState` thay vì `client` (refactor nhẹ), `_fire` gọi helper. Save `Destination` instead of `chat_id` ở `_meta`.

## Files

- `src/mcgram/tools/ask.py` — modify (reject ntfy)
- `src/mcgram/reminders.py` — refactor để dùng `send_message_to`
- `src/mcgram/server.py` — `ReminderScheduler(state, settings, audit)` thay vì `(client, settings, audit)`
- `src/mcgram/tools/_dispatch.py` — new (helper) — optional, có thể đặt ở `runtime.py`

## Tests

### ask (`tests/unit/test_ask_ntfy.py` or extend existing)
- [ ] `test_ask_on_ntfy_channel_returns_transport_unsupported_error_without_calling_client`
- [ ] `test_ask_on_ntfy_channel_audited_as_rejected`
- [ ] `test_ask_unknown_channel_takes_priority_over_transport_check`

### reminders (`tests/unit/test_reminders_ntfy.py` or extend)
- [ ] `test_reminder_on_ntfy_channel_fires_via_ntfy_client`
- [ ] `test_reminder_on_telegram_channel_still_fires_via_tg_client`
- [ ] `test_list_reminders_includes_transport_in_output` (nice-to-have)

## Acceptance

- `ask` ntfy → lỗi sạch, không 500, không gọi tg_client/ntfy_client
- Reminder firing pass cho cả 2 transport
- Existing reminder tests pass
