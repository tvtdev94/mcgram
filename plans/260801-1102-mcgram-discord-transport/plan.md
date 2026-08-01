---
title: "mcgram: Discord webhook transport (one-way, multi-channel)"
description: "Transport thứ ba `discord` cho mcgram — webhook một chiều, nhiều webhook ứng nhiều Discord channel, thread_id là tham số runtime."
status: completed
priority: P2
branch: "feat/discord-transport"
tags: [mcgram, discord, transport, mcp]
blockedBy: []
blocks: []
created: "2026-08-01T04:08:41.027Z"
createdBy: "ck:plan"
source: skill
status_note: "Implemented 2026-08-01 on branch feat/discord-transport, rebased onto origin/main 9a99dfa (v0.3.0, poll-ownership refactor). Version bumped to 0.4.0 (0.3.0 was already taken by the poll-ownership release). Shipped default display name is `mcgram` (neutral, chosen after code review flagged that a personal handle would ship as the public PyPI default; users set their own `discord.username` in config). User-facing strings kept English to match the codebase. `mcgram init` Discord step is TTY-gated (no-op in CI/tests). Full suite green except tests/test_multi_instance_stdio.py, a PRE-EXISTING flaky multi-process poll-ownership test that also fails on origin/main's own CI (unrelated to Discord). mypy strict shows ~23 PRE-EXISTING repo errors (non-blocking in CI); Discord code adds none."
---

# mcgram: Discord webhook transport (one-way, multi-channel)

## Overview

Thêm transport `discord` vào mcgram 0.3.0 (https://github.com/tvtdev94/mcgram), song song
với `telegram` và `ntfy` đang có. Một chiều: gửi được, không nhận (`ask` không hỗ trợ).

Hai quyết định định hình toàn bộ thiết kế:

1. **Nhiều webhook, mỗi cái một Discord channel.** Webhook Discord gắn cứng vào một channel
   và không đổi được, nên "channel" của mcgram map 1-1 với webhook. Dùng luôn `channels:`
   sẵn có thay vì dựng registry riêng — `resolve_destination()` đã làm đúng việc tra tên →
   transport → endpoint và báo lỗi kèm danh sách tên hợp lệ.
2. **`thread_id` là tham số runtime, KHÔNG nằm trong config.** Người dùng đưa thread ID lúc
   gọi tool. Hệ quả kỹ thuật: `Destination` là frozen dataclass dựng từ config nên
   `thread_id` phải đi qua chữ ký `dispatch.*` như tham số riêng — xem Phase 2.

## Quyết định đã chốt với người dùng

| Điểm | Chốt |
|---|---|
| Gọi "log discord" không kèm tên channel | Báo lỗi + liệt kê tên đã đăng ký. Không đoán, không default. |
| Display name | Global `discord.username`, mặc định `"Tuan Assistant"` (tên là gợi ý — một dòng config, đổi tự do). Không override per-channel, không override per-call. |
| CLI nhập webhook URL | Cả hai: `--webhook <url>` cho script, prompt ẩn khi thiếu cờ. Gắn thêm vào `mcgram init`. |
| Git identity / gh account | Không quan tâm — cook ở máy khác. |

## Kiến trúc mục tiêu

```yaml
# ~/.mcgram/config.yaml — KHÔNG chứa credential
discord:
  username: "Tuan Assistant"

channels:
  eve:
    transport: discord
    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_EVE
    description: "Eve product log"
  deploy:
    transport: discord
    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_DEPLOY
```

```bash
# ~/.mcgram/.env — chmod 600, credential nằm đây (đúng pattern bot.token_env)
MCGRAM_DISCORD_WEBHOOK_EVE=https://discord.com/api/webhooks/<id>/<token>
MCGRAM_DISCORD_WEBHOOK_DEPLOY=https://discord.com/api/webhooks/<id>/<token>
```

```
send_message(text="deploy xong", channel="eve", thread_id="1532959062499659987")
send_message(text="deploy xong", channel="eve")   # không thread → channel gốc
send_message(text="...")                          # → lỗi nếu default không phải discord
```

## Phases

| Phase | Name | Status |
|-------|------|--------|
| 1 | [Discord client + errors](./phase-01-discord-client-errors.md) | Completed |
| 2 | [Config + dispatch wiring](./phase-02-config-dispatch-wiring.md) | Completed |
| 3 | [Tools layer (runtime thread_id)](./phase-03-tools-layer-runtime-thread-id.md) | Completed |
| 4 | [CLI setup + doctor](./phase-04-cli-setup-doctor.md) | Completed |
| 5 | [Docs + tests + release](./phase-05-docs-tests-release.md) | Completed |

Phụ thuộc tuyến tính: 1 → 2 → 3 → 4 → 5. Phase 5 gộp test toàn cục + release.

## Acceptance criteria

- [ ] Đăng ký ≥2 Discord channel, gửi tới từng cái bằng tên, tin nhắn tới đúng nơi.
- [ ] `thread_id` truyền lúc gọi tool → tin vào đúng thread; bỏ trống → vào channel gốc.
- [ ] `thread_id` sai → lỗi có cấu trúc, nêu rõ nguyên nhân, không crash server.
- [ ] Tên channel sai → lỗi kèm danh sách tên hợp lệ.
- [ ] Display name hiện `Tuan Assistant` trên Discord.
- [ ] `mcgram doctor` kiểm được mọi Discord channel, không in webhook URL.
- [ ] Config telegram+ntfy cũ chạy y nguyên — không regression.
- [ ] Instance discord-only không giữ Telegram poll lock.
- [ ] Webhook URL không xuất hiện trong `audit.jsonl`, stdout, hay log.
- [ ] `pytest`, `ruff check`, `mypy` sạch.

## Ràng buộc xuyên suốt

- **Không đổi hành vi telegram/ntfy.** Config cũ phải boot y nguyên. Đây là ràng buộc
  cứng nhất — mọi sửa vào `config.py`/`server.py`/`dispatch.py` đều là đường dùng chung.
- **Không log credential.** `audit.jsonl` chỉ ghi `webhook_id` (phần số trong URL) +
  `thread_id`. CLI không in URL ra stdout. Doctor in `name`/`channel_id`, không in token.
- **YAGNI.** Chỉ một chiều. Không bot token, không tạo thread, không gateway, không
  `ask` qua Discord.
- Theo convention repo: `from __future__ import annotations`, type hint đầy đủ, docstring
  cùng style, tool handler trả dict có cấu trúc chứ không raise ra ngoài.

## Tham chiếu đã verify (không đoán)

Wire format đã test thật trên máy này ngày 2026-08-01:

- Vào thread: `thread_id` là **query string** `?thread_id=<id>`, KHÔNG nằm trong body.
  Đây là chỗ dễ sai nhất.
- Gửi file: `multipart/form-data`, field `payload_json` (JSON string) + `files[0]` (binary).
- Silent: `"flags": 4096` (SUPPRESS_NOTIFICATIONS) trong body.
- `?wait=true` → Discord trả message object có `id`.
- `GET <webhook_url>` → metadata `{name, channel_id, guild_id}`, dùng cho doctor.

Error code đã gặp thật:

| HTTP | code | Nghĩa |
|---|---|---|
| 404 | 10015 | Unknown Webhook — URL sai hoặc webhook đã xoá |
| 400 | 10003 | Unknown Channel — `thread_id` sai hoặc thread không thuộc channel của webhook |
| 400 | 220003 | Webhook chỉ tạo được thread trong forum channel |
| 400 | 220001 | Forum channel thiếu cả `thread_name` lẫn `thread_id` |
| 429 | — | Rate limit ~30 req/60s mỗi webhook; body có `retry_after` |

Giới hạn: `content` tối đa **2000 ký tự** (Telegram 4096 — xem Phase 3), file 25 MB.

Script tham chiếu wire format: `~/.local/bin/discord-push` (bash+curl+jq, đã verify
các đường: gửi channel, gửi thread, đính kèm file, thread sai, `thread_name` trên text channel).

Docs: https://docs.discord.com/developers/resources/webhook

## Môi trường cook

- Bản đang chạy để tham chiếu kiến trúc (ĐỌC, KHÔNG SỬA):
  `~/.local/share/pipx/venvs/mcgram/lib/python3.14/site-packages/mcgram/`
- Python 3.14. Deps: httpx, pydantic v2, mcp, pyyaml, python-dotenv.
  Dev: pytest, pytest-asyncio, pytest-httpx, ruff, mypy.
- Sau khi xong: `pipx install --force .` rồi restart Claude Code để MCP nạp tool mới.

## Dependencies

Không có plan nào khác trong `plans/` đang mở. Không có cross-plan dependency.

## Open questions

1. Repo có sẵn test cho ntfy transport để nhân theo không? Người cook đọc `tests/` xác nhận
   trước khi viết Phase 5; nếu chưa có mẫu thì tự dựng theo `pytest-httpx`.
2. Webhook URL trong `~/.discord-push/config` đã dán qua chat — nếu channel nhạy cảm thì
   xoá webhook ở Server Settings và tạo mới trước khi đưa vào `.env`.
