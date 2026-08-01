---
phase: 5
title: "Docs + tests + release"
status: completed
effort: ""
priority: P2
dependencies: [4]
---

# Phase 5: Docs + tests + release

## Overview

Đóng gói: docs người dùng, SKILL.md để Claude Code biết cách dùng, quét test toàn cục,
bump version, cài lại bản local.

## Requirements

**Functional**
- `config.example.yaml` có section discord kèm chú thích.
- `SKILL.md` nêu rõ Discord một chiều và cần tên channel.
- README liệt kê transport thứ ba.

**Non-functional**
- Test regression phủ đường telegram/ntfy.
- `pytest` / `ruff` / `mypy` sạch toàn repo.

## Architecture

`SKILL.md` là bề mặt Claude Code đọc để quyết định gọi tool nào — quan trọng ngang code.
Phải trả lời được ba câu:

1. Khi nào dùng discord thay vì telegram/ntfy? → khi người dùng nói "log discord",
   hoặc nêu tên channel Discord đã đăng ký.
2. Cần gì để gọi? → **bắt buộc** có tên channel; `thread_id` tuỳ chọn, chỉ khi người
   dùng cung cấp.
3. Cái gì không làm được? → không `ask` (một chiều), không tạo thread mới.

Viết ngắn gọn, đúng giọng SKILL.md hiện có.

## Related Code Files

- Modify: `mcgram/data/config.example.yaml`
- Modify: `mcgram/data/skill/SKILL.md`
- Modify: `README.md`
- Modify: `pyproject.toml` (0.3.0 → 0.4.0)
- Modify: `CHANGELOG.md` nếu repo có
- Create: `tests/test_discord_integration.py`

## Implementation Steps

1. **`config.example.yaml`** — thêm section, chú thích theo giọng file hiện tại:
   ```yaml
   # === Transport C: Discord (webhook, một chiều) =========================
   # Mỗi webhook gắn cứng vào một Discord channel. Khai một channel mcgram cho
   # mỗi webhook. URL KHÔNG bao giờ để ở đây — nó nằm trong ~/.mcgram/.env.
   # Dùng `mcgram channel add-discord --name <tên>` để thiết lập.
   #
   # discord:
   #   username: "Tuan Assistant"    # tên hiển thị trên Discord
   #   avatar_url:                   # tuỳ chọn
   #
   # channels:
   #   eve:
   #     transport: discord
   #     discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_EVE
   #
   # Gửi vào thread: truyền thread_id lúc gọi tool, không đặt trong config.
   # Webhook KHÔNG tạo được thread mới trong text channel — thread phải có sẵn.
   ```

2. **`SKILL.md`** — thêm mục Discord trả lời ba câu ở trên. Nêu rõ:
   - Gọi discord phải kèm tên channel; không có default, gọi thiếu → lỗi.
   - `thread_id` chỉ truyền khi người dùng đưa; không tự bịa.
   - `ask` không dùng được với discord.

3. **README** — thêm discord vào bảng/danh sách transport, thêm ví dụ setup ngắn.

4. **Test tích hợp** (`tests/test_discord_integration.py`) — đường xuyên suốt bằng
   `pytest-httpx`, config in-memory:
   - 2 discord channel → gửi tới từng cái, assert đúng URL từng request
   - có/không `thread_id` → assert query string
   - lỗi thread → error có cấu trúc, server không chết
   - `ask` trên discord → `unsupported_transport`

5. **Test regression** — chốt chặn quan trọng nhất:
   - Load config telegram+ntfy giống `~/.mcgram/config.yaml` thật → `default` channel
     resolve y hệt trước khi sửa
   - `send_message` không kèm `thread_id` tới telegram → payload không đổi
   - Instance discord-only không dựng `PollOwnership`
   - Bản ghi audit cho telegram/ntfy giữ nguyên các field cũ

6. **Quét bảo mật** — grep toàn bộ output test và fixture, khẳng định không có
   webhook token trong: `audit.jsonl`, stdout CLI, message lỗi, log server.
   Viết thành test thật chứ không kiểm bằng mắt.

7. **`pytest` + `ruff check` + `mypy`** toàn repo. Sửa hết, không bỏ qua bằng
   `# type: ignore` trừ khi có lý do ghi rõ.

8. **Bump version** `pyproject.toml` → `0.4.0`. Thêm transport là minor bump.
   Cập nhật `__version__` nếu nằm nơi khác.

9. **CHANGELOG** — nếu repo có, ghi mục 0.4.0: thêm Discord transport, và nêu thay đổi
   thứ tự kiểm tra trong `send_message` (resolve trước, đo độ dài sau).

10. **Cài lại bản local:**
    ```bash
    pipx install --force .
    ```
    Restart Claude Code để MCP server nạp tool mới. Xác nhận `send_message` có
    `thread_id` trong schema.

11. **Kiểm thủ công lần cuối** với webhook thật:
    - `mcgram doctor` → discord OK
    - gửi vào channel gốc
    - gửi vào thread `1532959062499659987`
    - đính kèm file
    - thread ID sai → lỗi rõ ràng

## Success Criteria

- [ ] `pytest` xanh toàn bộ
- [ ] `ruff check` sạch
- [ ] `mypy` sạch
- [ ] Test regression telegram/ntfy có mặt và xanh
- [ ] Test bảo mật khẳng định không rò token ở mọi đường
- [ ] `config.example.yaml` có section discord kèm chú thích
- [ ] `SKILL.md` trả lời được ba câu ở phần Architecture
- [ ] README nêu transport thứ ba
- [ ] Version 0.4.0
- [ ] `pipx install --force .` chạy được, MCP nạp tool có `thread_id`
- [ ] Kiểm thủ công 5 mục ở bước 11 đều đạt

## Risk Assessment

**SKILL.md viết mơ hồ = tính năng không tồn tại.** Model đọc file này để quyết định.
Nếu không nói rõ "bắt buộc có tên channel", model sẽ gọi thiếu và nhận lỗi liên tục.
Sau khi cài lại, thử một lượt hội thoại thật để xác nhận model gọi đúng.

**Kiểm bảo mật bằng mắt sẽ sót.** Phải là test tự động. Rò credential không tự lộ ra
lúc dùng bình thường — nó lộ khi người dùng gửi log đi để nhờ debug.

**Bản pipx cũ vẫn chạy.** Sau `pipx install --force .` mà không restart Claude Code
thì MCP server cũ vẫn sống, tool mới không thấy. Ghi rõ trong README.
