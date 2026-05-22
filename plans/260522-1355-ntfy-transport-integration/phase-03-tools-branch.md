# Phase 03 — Tools branch theo transport

**Priority:** P0
**Status:** PENDING
**Depends on:** phase-01 (NtfyClient), phase-02 (Destination)

## Overview

3 tool 1-chiều (`send_message`, `send_file`, `send_video`) phải branch theo `Destination.transport`. Logic chung (rate limit, audit, input validation) **không trùng lặp** — chỉ phần network call khác.

## Pattern

```python
async def handle(state: AppState, *, text: str, channel: str | None = None, ...):
    # ... validate input + rate limit (unchanged) ...
    dest = state.settings.resolve_destination(channel)
    t0 = time.monotonic()
    try:
        if dest.transport == "ntfy":
            assert state.ntfy_client is not None  # config guarantees
            result = await state.ntfy_client.send_message(
                dest.ntfy_topic, text, silent=silent,
            )
            message_id = result.get("id")  # ntfy returns "id"
        else:
            assert state.tg_client is not None
            msg = await state.tg_client.send_message(
                dest.chat_id, text, parse_mode=..., disable_notification=silent,
            )
            message_id = msg.get("message_id")
    except (TelegramError, NtfyError) as e:
        # ... audit error (transport-tagged) ...
        return {"error": f"{dest.transport}_api", "reason": str(e)}
    # ... audit ok with transport field ...
    return {"ok": True, "message_id": message_id, "channel": dest.name, "transport": dest.transport}
```

**Helper extraction:** nếu file vượt >200 LOC sau branch, trích `_send_via_destination(state, dest, ...)` ra module riêng `tools/_send_dispatch.py`. Mặc định inline trước, đo LOC sau.

## Files

- `src/mcgram/tools/send_message.py` — branch
- `src/mcgram/tools/send_file.py` — branch
- `src/mcgram/tools/send_video.py` — branch
- `src/mcgram/runtime.py` — `AppState`:
  - `tg_client: TelegramClient | None` (đổi từ `client`)
  - `ntfy_client: NtfyClient | None`
  - Property `client` giữ lại trong giai đoạn chuyển tiếp (alias `tg_client`) để legacy code/test không vỡ

## Audit fields mới

Mỗi entry add `"transport": "telegram" | "ntfy"`. Audit reader (`cli_audit.py`) optional show transport column.

## Tests (`tests/integration/test_tools_ntfy.py`)

Mock cả `NtfyClient` và `TelegramClient`:

- [ ] `test_send_message_routes_to_ntfy_when_channel_is_ntfy`
- [ ] `test_send_message_routes_to_telegram_when_channel_is_telegram`
- [ ] `test_send_message_ntfy_audit_includes_transport_field`
- [ ] `test_send_message_ntfy_silent_maps_to_priority_1`
- [ ] `test_send_message_unknown_channel_returns_error_without_calling_either_client`
- [ ] `test_send_file_routes_to_ntfy`
- [ ] `test_send_file_ntfy_propagates_caption`
- [ ] `test_send_file_too_large_rejected_before_ntfy_call` (re-use existing limit)
- [ ] `test_send_video_routes_to_ntfy`
- [ ] `test_telegram_api_error_audited_with_telegram_transport_tag`
- [ ] `test_ntfy_api_error_audited_with_ntfy_transport_tag`

## Regression

- Tất cả test hiện có (`test_send_message.py`, `test_send_file.py`, `test_send_video.py`) phải pass nguyên si — telegram path không đổi behavior.

## Acceptance

- 3 tool branch chính xác theo transport
- Audit entries gắn tag `transport`
- Tool file vẫn <200 LOC mỗi file (hoặc <250 nếu cần kèm switch — không trích sớm)
- All tests pass
