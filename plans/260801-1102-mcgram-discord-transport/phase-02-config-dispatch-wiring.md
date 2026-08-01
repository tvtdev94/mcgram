---
phase: 2
title: "Config + dispatch wiring"
status: completed
effort: ""
priority: P1
dependencies: [1]
---

# Phase 2: Config + dispatch wiring

## Overview

Đưa `discord` thành transport hạng nhất trong config, dispatch, runtime state và server
bootstrap. Đây là phase rủi ro nhất — mọi file đụng vào đều là đường dùng chung với
telegram/ntfy đang chạy.

## Requirements

**Functional**
- Khai nhiều Discord channel trong `channels:`, mỗi cái trỏ tới một env var chứa webhook URL.
- `resolve_destination(name)` trả `Destination` có webhook URL đã resolve.
- `dispatch.*` nhận `thread_id` như tham số runtime.

**Non-functional**
- Config chỉ có telegram+ntfy phải boot y nguyên, không đổi hành vi default channel.
- Instance discord-only không được giữ Telegram poll lock.

## Architecture

### `thread_id` không thuộc `Destination`

Điểm thiết kế quan trọng nhất của phase này. `Destination` là frozen dataclass dựng từ
config lúc resolve; `thread_id` do người dùng đưa lúc gọi tool. Hai vòng đời khác nhau.

Nên **không** nhét `thread_id` vào `Destination`. Thay vào đó mở rộng chữ ký `dispatch.*`:

```python
async def send_text(
    state: AppState,
    dest: Destination,
    text: str,
    *,
    silent: bool = False,
    parse_mode: str | None = None,
    thread_id: str | None = None,   # ← thêm; chỉ discord dùng
) -> dict[str, Any]:
```

Telegram/ntfy bỏ qua tham số này. Có thể thấy hơi lệch, nhưng đó là hệ quả trung thực
của việc Discord có tầng địa chỉ thứ hai mà hai transport kia không có. Cách còn lại —
tạo `Destination` mới mỗi lần gọi bằng `dataclasses.replace()` — làm mờ ranh giới
config/runtime và khiến audit log khó nói rõ cái gì từ config, cái gì từ lời gọi.

### Config schema

```python
class DiscordConfig(BaseModel):
    """Thiết lập chung cho mọi Discord channel."""
    username: str = "Tuan Assistant"
    avatar_url: str | None = None
```

Không có `default_channel`, không có `webhooks:` registry riêng — người dùng đã chốt:
gọi không kèm tên channel thì báo lỗi, và `channels:` đã đủ vai trò registry.

`ChannelConfig` thêm:

```python
discord_webhook_env: str | None = None
```

Validator: `transport == "discord"` bắt buộc có `discord_webhook_env`, kèm thông báo
nói rõ phải đặt biến đó trong `~/.mcgram/.env`.

`Destination` (frozen) thêm:

```python
discord_webhook_url: str | None = None
discord_username: str | None = None
discord_avatar_url: str | None = None
```

`Settings` thêm `discord: DiscordConfig | None = None`.

### Không đổi hành vi default channel

`_seed_default_channel()` hiện ưu tiên telegram (khi polling bật) → ntfy. **Giữ nguyên
thứ tự đó.** Discord chỉ được seed làm default khi nó là transport duy nhất được cấu hình —
tức không có `bot` lẫn `ntfy`. Người dùng đã chốt "không kèm tên channel thì báo lỗi",
nên hành vi này chỉ là lối thoát cho cấu hình discord-only, không phải tính năng.

`_require_at_least_one_transport()` nới ra: chấp nhận discord đứng một mình.

## Related Code Files

- Modify: `mcgram/config.py` — `Transport`, `DiscordConfig`, `ChannelConfig`,
  `Destination`, `Settings`, `resolve_destination`, `_seed_default_channel`,
  `_require_at_least_one_transport`
- Modify: `mcgram/dispatch.py` — `_require_client`, `send_text`, `send_document`, `send_video_file`
- Modify: `mcgram/runtime.py` — `AppState.discord_client`
- Modify: `mcgram/server.py` — `_build_clients`, `_start_polling`
- Create: `tests/test_config_discord.py`

## Implementation Steps

1. **`config.py` — `Transport`** → `Literal["telegram", "ntfy", "discord"]`.

2. **`DiscordConfig`** — như trên. Validator cho `avatar_url`: nếu có thì phải bắt đầu
   bằng `http://` hoặc `https://` (theo mẫu `NtfyConfig._check_server`).

3. **`ChannelConfig`** — thêm `discord_webhook_env`. Mở rộng
   `_validate_per_transport()`:
   ```python
   if self.transport == "discord" and not self.discord_webhook_env:
       raise ValueError(
           "discord channel requires discord_webhook_env "
           "(tên biến env chứa webhook URL, đặt trong ~/.mcgram/.env)"
       )
   ```

4. **`Destination`** — thêm 3 field discord như trên.

5. **`Settings.discord`** — thêm field. Cập nhật `_require_at_least_one_transport`
   để discord tính là một transport hợp lệ.

6. **`resolve_destination()` nhánh discord:**
   ```python
   if ch.transport == "discord":
       env_name = ch.discord_webhook_env
       url = os.environ.get(env_name) if env_name else None
       if not url:
           raise ConfigError(
               f"discord channel {key!r}: biến env {env_name!r} chưa được đặt "
               f"(kiểm tra ~/.mcgram/.env)"
           )
       dc = self.discord or DiscordConfig()
       return Destination(
           name=key,
           transport="discord",
           discord_webhook_url=url,
           discord_username=dc.username,
           discord_avatar_url=dc.avatar_url,
       )
   ```
   Lưu ý: `ConfigError` nêu **tên biến**, không bao giờ nêu giá trị.

7. **`_seed_default_channel()`** — thêm nhánh cuối: chỉ khi `bot is None and ntfy chưa
   dùng được` và có đúng cấu hình discord thì mới seed. Không chen vào trước telegram/ntfy.
   Viết test khẳng định thứ tự cũ không đổi.

8. **`dispatch.py` — `_require_client()`** thêm:
   ```python
   if dest.transport == "discord" and state.discord_client is None:
       raise ConfigError(
           f"channel {dest.name!r} dùng transport discord nhưng "
           "Discord client chưa khởi tạo"
       )
   ```

9. **`dispatch.send_text()`** — thêm kwarg `thread_id`, thêm nhánh:
   ```python
   if dest.transport == "discord":
       assert state.discord_client is not None
       assert dest.discord_webhook_url is not None
       result = await state.discord_client.send_message(
           dest.discord_webhook_url, text,
           thread_id=thread_id,
           username=dest.discord_username,
           avatar_url=dest.discord_avatar_url,
           silent=silent,
       )
       return {"message_id": result.get("id")}
   ```

10. **`send_document()` / `send_video_file()`** — tương tự, thêm kwarg `thread_id`,
    truyền `caption` vào tham số `content` của client.

11. **`runtime.py`** — `discord_client: DiscordClient | None = None` trong `AppState`,
    kèm comment giải thích như hai transport kia đang có.

12. **`server.py` — `_build_clients()`:** dựng `DiscordClient` khi
    `settings.discord is not None` **HOẶC** có bất kỳ channel nào `transport == "discord"`.
    Điều kiện thứ hai quan trọng: người dùng có thể khai channel mà bỏ qua section
    `discord:` (vì mọi field đều có default).
    Client không cần credential lúc dựng nên không có đường lỗi token như Telegram.

13. **`server.py` — `_start_polling()`:** kiểm tra kỹ. Hàm này đã return `(None, None)`
    khi `state.client is None`, và Discord không đụng `state.client`, nên **về lý thuyết
    đã đúng**. Phải viết test khẳng định: instance discord-only không gọi
    `PollOwnership(...)`. Nếu sai chỗ này, nó chiếm lock và chặn instance Telegram
    chạy `ask` — lỗi khó lần ra vì biểu hiện ở process khác.

## Success Criteria

- [ ] Khai 2 discord channel với 2 env var khác nhau → `resolve_destination` trả đúng URL từng cái
- [ ] Env var chưa set → `ConfigError` nêu **tên biến**, không nêu giá trị
- [ ] `transport: discord` mà thiếu `discord_webhook_env` → lỗi validate lúc load config
- [ ] Tên channel sai → `ConfigError` kèm danh sách tên hợp lệ (hành vi sẵn có, giữ nguyên)
- [ ] `dispatch.send_text(..., thread_id="123")` → client nhận đúng `thread_id`
- [ ] `thread_id=None` → client nhận None, không phải chuỗi rỗng
- [ ] **Regression:** config telegram+ntfy (đúng như `~/.mcgram/config.yaml` hiện tại)
      cho ra `default` channel y hệt trước khi sửa
- [ ] Config chỉ có discord vẫn boot; `default` seed về discord
- [ ] Instance discord-only KHÔNG dựng `PollOwnership`
- [ ] `mypy` sạch (`Destination` frozen + field optional dễ sinh lỗi type)

## Risk Assessment

**Cao — đụng đường dùng chung.** `config.py` và `server.py` phục vụ cả 3 transport.
Giảm rủi ro: viết test regression cho cấu hình telegram+ntfy **trước** khi sửa, chạy
để thấy xanh, rồi mới sửa. Test đó là lưới an toàn.

**Poll lock.** Nếu discord-only instance chiếm lock, triệu chứng xuất hiện ở process
Telegram khác (mất `ask`), không ở process gây lỗi. Test trực tiếp bằng cách assert
`_start_polling()` trả `(None, None)`.

**Env var đọc lúc nào.** `resolve_destination()` đọc `os.environ` mỗi lần gọi, không
cache — đúng với `Settings.resolve_token()` hiện tại. Giữ vậy: `.env` được
`_load_env_beside_config()` nạp lúc boot, sửa `.env` cần restart server. Ghi rõ trong docs.
