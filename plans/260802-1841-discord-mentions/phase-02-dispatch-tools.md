# Phase 02 — Dispatch + tools (mention param end-to-end)

## Requirements

Expose param `mention: list[str]` trên 3 tool gửi; resolve tên→id; prepend `<@id>` vào content; length-check gồm prefix; thread `mention_user_ids` xuống client.

## Files to modify

- `src/mcgram/dispatch.py`
- `src/mcgram/tools/send_message.py`, `send_file.py`, `send_video.py`
- `tests/test_tools_discord.py`

## Steps

### dispatch.py

1. `send_text`, `send_document`, `send_video_file`: thêm param `mention_user_ids: list[str] | None = None`. Chỉ truyền vào nhánh `discord` (telegram/ntfy bỏ qua):
   `state.discord_client.send_message(..., mention_user_ids=mention_user_ids)` (tương tự send_file/send_video).

### Shared logic trong tools (lặp lại nhất quán, không tạo abstraction thừa)

Sau khi có `dest`, trước length/caption check:
```python
from ..discord_client import format_mention_prefix, resolve_mentions, webhook_id_from_url
mention_ids: list[str] = []
mention_note: str | None = None
if mention:
    if dest.transport == "discord":
        ids, unknown = resolve_mentions(mention, dest.discord_mentions or {})
        if unknown:
            state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                               "reason": "unknown_mention", "unknown": unknown})
            return {"error": "invalid_input", "reason": "unknown_mention",
                    "unknown": unknown, "known": sorted(dest.discord_mentions or {})}
        mention_ids = ids
    else:
        mention_note = f"mention ignored for {dest.transport}"
```

### send_message.py

2. Signature: thêm `mention: list[str] | None = None`.
3. Sau resolve dest + block trên: build content
   ```python
   content = format_mention_prefix(mention_ids) + text if mention_ids else text
   ```
   Đổi length-check dùng `content` thay `text` (`len(content) > max_text` → `text_too_long`).
4. Gọi `send_text(state, dest, content, ..., mention_user_ids=mention_ids or None)`.
5. Audit `text`/`text_len` dùng `content`. Nếu `mention_ids`: `record["mentions"] = list(mention)`.
6. Result: nếu `mention_note`: thêm `result["note"]` (gộp với note thread_id nếu cả hai — ưu tiên nối 2 note bằng "; ").
7. Schema: thêm property `mention` (array of string) + mô tả Discord-only.

### send_file.py / send_video.py

8. Signature: thêm `mention`.
9. Build caption có mention: `caption = format_mention_prefix(mention_ids) + (caption or "")` khi có mention_ids — TRƯỚC bước truncation `caption_max_chars`.
10. Truyền `mention_user_ids=mention_ids or None` vào `send_document`/`send_video_file`.
11. Note + schema tương tự send_message.

## Tests (test_tools_discord.py)

- `mention=["alice"]` (alice đã đăng ký) → request body `content` bắt đầu `<@ID>`, `allowed_mentions.users==[ID]`.
- `mention=["ghost"]` → `unknown_mention`, `known` liệt kê, KHÔNG có request network.
- `mention` trên telegram → `ok` + `note` chứa "ignored".
- Length: `text="x"*1995` + 1 mention (prefix ~ `<@…> `) vượt 2000 → `text_too_long`.
- send_file với `mention` → caption/content có prefix + `allowed_mentions.users`.
- Regression: message Discord không mention vẫn `allowed_mentions={"parse":[]}`.
- Audit: token webhook vẫn không lộ; record có `mentions` khi dùng.

## Validation

`uv run pytest tests/test_tools_discord.py tests/test_discord_integration.py -q`
