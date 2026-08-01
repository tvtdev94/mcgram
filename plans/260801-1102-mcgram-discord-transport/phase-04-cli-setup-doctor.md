---
phase: 4
title: "CLI setup + doctor"
status: completed
effort: ""
priority: P2
dependencies: [3]
---

# Phase 4: CLI setup + doctor

## Overview

Đường setup cho người dùng: đăng ký Discord channel bằng CLI, ghi credential vào `.env`
đúng cách, kiểm tra kết nối bằng `mcgram doctor`, và hỏi trong `mcgram init`.

## Requirements

**Functional**
- `mcgram channel add-discord --name <tên> [--webhook <url>]` — thiếu cờ thì prompt ẩn.
- `mcgram channel list` hiển thị discord channel mà không lộ URL.
- `mcgram doctor` kiểm mọi discord channel.
- `mcgram init` hỏi có thêm Discord không.

**Non-functional**
- Webhook URL không bao giờ xuất hiện trên stdout hay trong shell history.
- `.env` giữ `chmod 600` sau mỗi lần ghi.

## Architecture

### Ghi credential vào `.env`

Đây là lần đầu mcgram **ghi** vào `.env` (hiện `cli_init.py` có thể đã làm — người cook
đọc xác nhận và tái dùng helper nếu có, đừng viết trùng). Yêu cầu:

- Tạo file với `0o600` nếu chưa có; giữ nguyên quyền nếu đã có.
- Cập nhật tại chỗ khi key đã tồn tại, không nhân bản dòng.
- Không nuốt các dòng khác (`MCGRAM_BOT_TOKEN` phải nguyên vẹn).
- Viết qua file tạm rồi `os.replace()` để không hỏng file khi bị ngắt giữa chừng.

### Đặt tên env var

Từ tên channel: `eve` → `MCGRAM_DISCORD_WEBHOOK_EVE`. Chuẩn hoá: upper-case, ký tự
không hợp lệ thành `_`. Cho phép `--env-name` để đặt tay khi cần.

### Nhập webhook URL

Cả hai đường (người dùng đã chốt):

```bash
mcgram channel add-discord --name eve --webhook https://...   # script
mcgram channel add-discord --name eve                          # prompt ẩn
```

Prompt dùng `getpass.getpass("Discord webhook URL: ")` — không hiện lên màn hình,
không vào history. Cảnh báo khi dùng `--webhook`: nhắc URL sẽ nằm trong shell history.

### Xác thực trước khi ghi

`GET` webhook URL trước khi lưu. Hợp lệ → in `name` + `channel_id` để người dùng
xác nhận đúng channel. Sai → không ghi gì, báo lỗi. Điều này bắt lỗi copy nhầm ngay
lập tức thay vì để phát hiện lúc gửi tin đầu tiên.

## Related Code Files

- Modify: `mcgram/cli_channel.py` — `cmd_add_discord`, `_endpoint_summary`, argparse
- Modify: `mcgram/cli_doctor.py` — `_check_discord`
- Modify: `mcgram/cli_init.py` — bước hỏi Discord
- Create hoặc reuse: helper ghi `.env` (kiểm tra `cli_init.py` trước)
- Create: `tests/test_cli_discord.py`

## Implementation Steps

1. **Đọc `cli_channel.py`** — nắm `cmd_add_ntfy` (mẫu gần nhất), `_endpoint_summary`,
   cách nạp/ghi YAML, cách bảo vệ channel `default`.

2. **`_endpoint_summary()`** — nhánh discord trả `f"env={ch['discord_webhook_env']}"`.
   **Không bao giờ resolve và in giá trị.** Đây là hàm dùng cho `channel list` in ra
   màn hình.

3. **`cmd_add_discord()`:**
   - Chặn tên `default` (giống `cmd_add_ntfy` đang làm).
   - Lấy URL: từ `--webhook` hoặc `getpass`. Trống → lỗi.
   - Kiểm dạng URL: phải khớp `https://discord.com/api/webhooks/<id>/<token>`
     (chấp nhận `discordapp.com` cho URL cũ). Sai dạng → báo lỗi kèm ví dụ đúng.
   - Xác thực qua `DiscordClient.health()`. Thất bại → không ghi, thoát mã lỗi.
   - Thành công → in `webhook: <name> → channel <channel_id>` (không in URL).
   - Suy ra tên env var (hoặc lấy từ `--env-name`), ghi vào `.env`.
   - Ghi channel entry vào `config.yaml`.
   - Seed `discord:` section với `username: "Tuan Assistant"` nếu chưa có.
   - In hướng dẫn tiếp theo: restart Claude Code để MCP nạp config mới.

4. **argparse** — parser con `add-discord` với `--name` (bắt buộc), `--webhook`,
   `--env-name`, `--description`. Đăng ký vào dict dispatch cạnh `add-ntfy`.

5. **`cli_doctor.py` — `_check_discord()`:**
   Duyệt mọi channel `transport == "discord"`, mỗi cái:
   - env var đã set chưa → `_ok`/`_fail` (nêu tên biến, không nêu giá trị)
   - `health()` → in `name` + `channel_id` khi 200
   - gửi tin test `"mcgram doctor: discord OK — <tên channel>"`
   - báo `message_id` khi thành công

   Không gửi vào thread (config không giữ thread_id). Đếm số lỗi trả về như
   `_check_telegram`/`_check_ntfy` đang làm.

6. **`cli_init.py`** — sau bước telegram/ntfy, hỏi "Thêm Discord channel?".
   Nếu có → hỏi tên channel, prompt URL ẩn, gọi lại chính logic của `cmd_add_discord`
   (tách thành hàm dùng chung, đừng copy). Cho phép lặp để thêm nhiều channel.

7. **Kiểm quyền `.env`** — sau khi ghi, `os.chmod(path, 0o600)`. Test khẳng định.

## Success Criteria

- [ ] `add-discord --name eve --webhook <url hợp lệ>` → ghi `.env` + `config.yaml`, in channel name
- [ ] `add-discord --name eve` (không cờ) → prompt ẩn, không echo
- [ ] Webhook sai → không ghi gì vào cả `.env` lẫn `config.yaml`
- [ ] URL sai định dạng → báo lỗi kèm ví dụ, không gọi mạng
- [ ] Ghi `.env` giữ nguyên `MCGRAM_BOT_TOKEN` và các dòng khác
- [ ] `.env` là `0o600` sau khi ghi
- [ ] Thêm cùng tên channel hai lần → cập nhật tại chỗ, không nhân bản
- [ ] `channel list` hiển thị discord channel, **không có URL trong output**
- [ ] `doctor` kiểm được nhiều discord channel, gửi tin test tới từng cái
- [ ] `doctor` output không chứa webhook URL
- [ ] `add-discord --name default` → từ chối
- [ ] `init` thêm được Discord, lặp được nhiều channel

## Risk Assessment

**Ghi hỏng `.env`.** File này chứa `MCGRAM_BOT_TOKEN` — ghi đè sai làm hỏng Telegram.
Giảm rủi ro: ghi qua file tạm + `os.replace()` (atomic), và test riêng cho việc bảo toàn
các dòng có sẵn.

**Rò URL qua stdout.** `channel list` và `doctor` chạy trong terminal, người dùng hay
paste output khi hỏi hỗ trợ. Test phải assert output **không chứa** token, chứ không chỉ
assert có chứa thông tin mong đợi.

**`getpass` trong môi trường không phải TTY.** CI hoặc pipe sẽ ném lỗi. Bắt và báo
"dùng `--webhook` khi chạy không tương tác".
