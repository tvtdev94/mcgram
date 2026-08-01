---
phase: 1
title: "Discord client + errors"
status: completed
effort: ""
priority: P1
dependencies: []
---

# Phase 1: Discord client + errors

## Overview

Dựng `DiscordClient` — HTTP client async một chiều cho Discord webhook, cùng
`DiscordError` trong exception hierarchy. Phase này độc lập hoàn toàn với config/dispatch,
test được riêng bằng `pytest-httpx`.

## Requirements

**Functional**
- Gửi text, gửi file, kiểm tra webhook còn sống.
- Hỗ trợ `thread_id` (query string), `username`, `avatar_url`, `silent`.
- Map error code Discord thành thông báo người dùng hiểu được.

**Non-functional**
- Mô phỏng shape của `NtfyClient`: async context manager, `httpx.AsyncClient`,
  `_request()` gom lỗi tập trung.
- Không giữ credential ở tầng client — webhook URL truyền vào từng lời gọi.

## Architecture

Khác biệt cốt lõi so với `TelegramClient`/`NtfyClient`: hai cái kia nhận credential
lúc khởi tạo (bot token / ntfy access token) vì credential dùng chung cho mọi đích.
Discord thì **mỗi webhook URL vừa là địa chỉ vừa là credential**, và mcgram có nhiều
webhook. Nên `DiscordClient()` khởi tạo rỗng, URL đi vào từng method.

```python
class DiscordClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def send_message(
        self,
        webhook_url: str,
        content: str,
        *,
        thread_id: str | None = None,
        username: str | None = None,
        avatar_url: str | None = None,
        silent: bool = False,
    ) -> dict[str, Any]: ...

    async def send_file(
        self,
        webhook_url: str,
        path: Path,
        *,
        content: str | None = None,
        thread_id: str | None = None,
        username: str | None = None,
        avatar_url: str | None = None,
        silent: bool = False,
    ) -> dict[str, Any]: ...

    async def send_video(...) -> dict[str, Any]:
        """Discord không phân biệt video/file — gọi lại send_file."""

    async def health(self, webhook_url: str) -> dict[str, Any] | None:
        """GET webhook URL. Trả metadata {name, channel_id, guild_id} hoặc None."""
```

**Wire format** (đã verify thật, không đoán — xem `~/.local/bin/discord-push`):

```
POST <webhook_url>?wait=true[&thread_id=<id>]
Content-Type: application/json
{"content": ..., "username": ..., "avatar_url": ..., "flags": 4096}
```

- `thread_id` là **query string**, không nằm trong body. Sai chỗ này là bug im lặng:
  Discord bỏ qua field lạ trong body, tin nhắn vào channel gốc thay vì thread.
- `?wait=true` để lấy `id` của message trả về — `message_id` trong kết quả tool cần giá trị thật.
- `flags: 4096` = SUPPRESS_NOTIFICATIONS khi `silent=True`.
- Trường `username`/`avatar_url` chỉ gửi khi khác None (Discord không chấp nhận null).

File upload dùng `multipart/form-data`:

```
POST <webhook_url>?wait=true[&thread_id=<id>]
  payload_json = <JSON string, giống body ở trên>
  files[0]     = <binary>
```

## Related Code Files

- Create: `mcgram/discord_client.py`
- Modify: `mcgram/errors.py`
- Create: `tests/test_discord_client.py`
- Read (mẫu): `mcgram/ntfy_client.py`, `mcgram/tg_client.py`

## Implementation Steps

1. **`errors.py`** — thêm `DiscordError(MCGramError)` với `status: int` +
   `description: str`, đúng shape `NtfyError`. Thêm field `code: int | None`
   cho Discord error code (10015, 220003…) vì nó chi tiết hơn HTTP status.

2. **`discord_client.py` khung** — `__aenter__`/`__aexit__` dựng/đóng
   `httpx.AsyncClient` với timeout giống ntfy (`httpx.Timeout(30.0, connect=10.0)`).
   Raise `RuntimeError` nếu dùng ngoài `async with` — y hệt `NtfyClient`.

3. **`_build_payload()`** — helper dựng dict body từ content/username/avatar_url/silent.
   Bỏ key có giá trị None. Dùng chung cho cả text lẫn multipart.

4. **`_build_url()`** — helper ghép query string: luôn có `wait=true`,
   thêm `thread_id` khi có. Dùng `httpx.URL(...).copy_merge_params()` hoặc
   `urlencode` — không nối chuỗi tay (webhook URL có thể đã mang query sẵn).

5. **`_request()`** — gom xử lý response tập trung:
   - 2xx → parse JSON, trả dict. Body rỗng (khi không có `wait=true`) → `{"status": code}`.
   - 429 → đọc `retry_after` từ JSON body, raise `DiscordError` có nêu số giây.
   - ≥400 → parse `{"message", "code"}` từ body Discord, raise `DiscordError(status, message, code)`.
   - Giới hạn độ dài `description` (200 ký tự) như `NtfyClient` đang làm.

6. **`_friendly_reason(code, message)`** — map error code sang câu giải thích tiếng người.
   Đây là phần tạo giá trị thật cho người dùng, đừng bỏ qua:

   | code | Thông báo |
   |---|---|
   | 10015 | Webhook không tồn tại hoặc đã bị xoá — kiểm tra lại URL trong `.env` |
   | 10003 | `thread_id` không đúng, hoặc thread không nằm trong channel của webhook này |
   | 220003 | Webhook không tạo được thread trong text channel (chỉ forum channel làm được). Cung cấp `thread_id` của thread đã có sẵn. |
   | 220001 | Channel này là forum — cần `thread_id` của post đã có |
   | 429 | Vượt rate limit (~30 req/60s mỗi webhook), thử lại sau N giây |

7. **`send_message`** — dựng URL + payload, POST JSON.

8. **`send_file`** — đọc file binary, POST multipart với `payload_json` + `files[0]`.
   Đoán MIME bằng `mimetypes.guess_type` cho đẹp nhưng không bắt buộc.

9. **`send_video`** — gọi thẳng `send_file`. Ghi docstring nêu rõ Discord tự nhận
   diện video theo phần mở rộng, không cần endpoint riêng.

10. **`health`** — `GET <webhook_url>` (không query string). 200 → trả dict metadata.
    404/401 → trả None thay vì raise, để `doctor` tự quyết cách hiển thị.

## Success Criteria

- [ ] `thread_id` xuất hiện trong **query string** của request, không trong body — test assert URL
- [ ] `wait=true` luôn có mặt; response `id` được trả về
- [ ] Multipart upload có đúng 2 phần: `payload_json` + `files[0]`
- [ ] `silent=True` → body có `flags: 4096`; `silent=False` → không có key `flags`
- [ ] `username=None` → body không có key `username` (không phải `null`)
- [ ] Mọi error code trong bảng map sang `DiscordError` có `code` + thông báo tiếng người
- [ ] 429 → `DiscordError` nêu `retry_after`
- [ ] `health()` trả metadata khi 200, trả None khi 404 (không raise)
- [ ] Dùng client ngoài `async with` → `RuntimeError`
- [ ] `ruff check` + `mypy` sạch trên file mới

## Risk Assessment

**Nối query string sai khi webhook URL đã có sẵn query.** Nối chuỗi tay `url + "?wait=true"`
sẽ hỏng. Dùng API parse URL đúng cách. Test riêng một case URL có sẵn `?`.

**Body rỗng khi không `wait=true`.** Discord trả 204 No Content — `resp.json()` sẽ ném
`ValueError`. `NtfyClient` đã xử lý bằng try/except, copy đúng pattern đó.

**`thread_id` là string, không phải int.** Discord snowflake vượt 2^53, để int trong JSON
sẽ mất chính xác ở một số parser. Giữ nguyên string xuyên suốt — đừng ép kiểu.
