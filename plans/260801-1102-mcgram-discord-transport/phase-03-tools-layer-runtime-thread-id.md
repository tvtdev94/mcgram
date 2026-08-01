---
phase: 3
title: "Tools layer (runtime thread_id)"
status: completed
effort: ""
priority: P1
dependencies: [2]
---

# Phase 3: Tools layer (runtime thread_id)

## Overview

Mở `thread_id` ra bề mặt MCP tool để Claude Code truyền vào lúc gọi, xử lý giới hạn
2000 ký tự của Discord, và chặn `ask` trên transport một chiều.

## Requirements

**Functional**
- `send_message` / `send_file` / `send_video` nhận `thread_id` tuỳ chọn.
- Giới hạn độ dài text resolve theo transport, không hardcode toàn cục.
- `ask` trên discord channel → lỗi có cấu trúc, không crash.

**Non-functional**
- Audit log ghi `webhook_id` + `thread_id`, tuyệt đối không ghi webhook token.
- Mô tả tool đủ rõ để model tự biết khi nào cần `thread_id` mà không phải hỏi lại.

## Architecture

### Giới hạn độ dài theo transport

`tools/send_message.py` đang hardcode `_MAX_TEXT = 4096` (cap của Telegram) và reject
khi vượt. Discord chỉ cho 2000. Đây là xung đột thật, không né được.

Chốt: **reject sớm**, không truncate — nhất quán với hành vi hiện tại của tool, và
không im lặng cắt dữ liệu người dùng. Nhưng phải resolve theo transport:

```python
_MAX_TEXT_BY_TRANSPORT = {
    "telegram": 4096,
    "ntfy": 4096,     # ntfy cho nhiều hơn; giữ nguyên cap cũ để không đổi hành vi
    "discord": 2000,  # Discord hard limit
}
```

Thứ tự quan trọng: phải `resolve_destination()` **trước** rồi mới kiểm tra độ dài, vì
giới hạn phụ thuộc transport. Hiện tại code kiểm tra độ dài trước khi resolve — cần
đảo lại. Đổi thứ tự làm thay đổi mã lỗi trả về khi vừa sai tên channel vừa quá dài:
giờ sẽ báo `unknown_channel` trước. Chấp nhận được và hợp lý hơn.

### `thread_id` trong tool schema

```json
"thread_id": {
  "type": "string",
  "description": "Discord only: ID của thread đã tồn tại để gửi vào. Bỏ trống → gửi vào channel gốc. Telegram/ntfy bỏ qua."
}
```

Kiểu **string**, không phải integer — snowflake của Discord vượt 2^53.

Khi `thread_id` được truyền cho channel không phải discord: bỏ qua im lặng hay báo lỗi?
Chọn **bỏ qua kèm ghi chú trong kết quả trả về** (`{"note": "thread_id ignored for <transport>"}`),
vì model có thể truyền thừa và làm hỏng một thông báo chỉ vì tham số dư là quá gắt.

### Audit

Ghi `discord_webhook_id` (phần số đầu trong URL) + `thread_id`, không ghi token.
Viết helper trong `discord_client.py` hoặc `dispatch.py`:

```python
def webhook_id_from_url(url: str) -> str | None:
    """Trích webhook ID từ URL để audit. KHÔNG BAO GIỜ trả về token."""
    # .../webhooks/<id>/<token> → <id>
```

## Related Code Files

- Modify: `mcgram/tools/send_message.py`
- Modify: `mcgram/tools/send_file.py`
- Modify: `mcgram/tools/send_video.py`
- Modify: `mcgram/tools/ask.py`
- Modify: `mcgram/discord_client.py` (thêm `webhook_id_from_url`)
- Create: `tests/test_tools_discord.py`

## Implementation Steps

1. **`send_message.py` — schema:** thêm property `thread_id` (string, mô tả như trên).
   Cập nhật `description` của tool để nhắc: gọi Discord phải nêu tên channel.

2. **`send_message.py` — đảo thứ tự kiểm tra:**
   ```python
   # (1) resolve trước — cần transport để biết giới hạn
   try:
       dest = state.settings.resolve_destination(channel)
   except ConfigError as e:
       ...  # unknown_channel, giữ nguyên
   # (2) rồi mới kiểm tra độ dài theo transport
   max_text = _MAX_TEXT_BY_TRANSPORT.get(dest.transport, 4096)
   if len(text) > max_text:
       return {"error": "invalid_input", "reason": "text_too_long",
               "max": max_text, "transport": dest.transport}
   ```
   Giữ nguyên check `text` rỗng ở đầu (không phụ thuộc transport).

3. **`send_message.py` — truyền xuống:** `thread_id` vào `send_text(...)`.
   Khi transport khác discord và `thread_id` có giá trị → thêm `note` vào dict trả về.

4. **`send_message.py` — audit:** thêm `discord_webhook_id` + `thread_id` vào record.
   Kiểm lại record không chứa URL đầy đủ ở bất kỳ đường nào (kể cả nhánh error —
   `DiscordError.description` phải sạch, Discord không echo URL trong body nhưng
   phải khẳng định bằng test).

5. **`send_file.py`** — thêm `thread_id` vào schema + handle. Giới hạn kích thước file
   resolve theo transport, giống pattern `ntfy_file_max_bytes` đang có:
   thêm `discord_file_max_bytes: int = Field(26_214_400, ge=1)` vào `LimitsConfig`,
   effective = `min(file_max_bytes, discord_file_max_bytes)`.
   Giữ nguyên guard `allow_outside_cwd` — không nới lỏng.

6. **`send_video.py`** — tương tự `send_file`.

7. **`ask.py`** — thêm guard sớm:
   ```python
   if dest.transport == "discord":
       return {
           "error": "unsupported_transport",
           "reason": "discord là transport một chiều, không nhận phản hồi. "
                     "Dùng channel telegram cho ask.",
           "channel": dest.name,
       }
   ```
   Đặt sau `resolve_destination()`, trước mọi thao tác registry. Cập nhật mô tả tool
   `ask` nêu rõ chỉ telegram hỗ trợ.

8. **`webhook_id_from_url()`** — parse an toàn, trả None khi URL không đúng dạng
   (không raise — đây là đường audit, không được làm hỏng lời gọi đang thành công).

## Success Criteria

- [ ] `send_message(channel="eve", thread_id="1532...")` → tin vào đúng thread
- [ ] `send_message(channel="eve")` → tin vào channel gốc
- [ ] `send_message(text=...)` không kèm channel, default là telegram → vẫn chạy như cũ
- [ ] Text 2001 ký tự tới discord → `text_too_long` với `max: 2000`
- [ ] Text 2001 ký tự tới telegram → vẫn gửi bình thường (cap 4096)
- [ ] Sai tên channel + text quá dài → báo `unknown_channel` trước
- [ ] `thread_id` truyền cho telegram channel → gửi bình thường, kèm `note`
- [ ] `ask` trên discord channel → `unsupported_transport`, không treo, không crash
- [ ] File > `discord_file_max_bytes` → reject trước khi gọi mạng
- [ ] **Audit không chứa webhook token** — test grep record, khẳng định không có
      substring của token; chỉ có `discord_webhook_id`
- [ ] Regression: gọi telegram/ntfy không truyền `thread_id` cho ra record audit
      giống hệt trước khi sửa

## Risk Assessment

**Đảo thứ tự resolve/length-check đổi mã lỗi.** Khi vừa sai channel vừa quá dài, giờ
báo `unknown_channel` thay vì `text_too_long`. Không ai phụ thuộc thứ tự này (nội bộ,
chưa có consumer bên ngoài) nhưng ghi vào CHANGELOG cho minh bạch.

**Rò credential qua audit.** Rủi ro nghiêm trọng nhất của phase. `audit.jsonl` không
tự xoay vòng bí mật và người dùng có thể gửi file đó đi khi debug. Test phải chủ động
grep token, không chỉ kiểm tra field mong đợi có mặt.

**Model không biết truyền `thread_id`.** Mô tả tool là bề mặt duy nhất model nhìn thấy.
Viết mô tả nêu rõ khi nào cần và định dạng — nếu mô tả mơ hồ, tính năng coi như không tồn tại.
