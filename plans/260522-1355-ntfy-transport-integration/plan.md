# Plan: ntfy.sh transport integration

**Date:** 2026-05-22
**Slug:** ntfy-transport-integration
**Status:** PENDING REVIEW

## Goal

Cho phép mcgram dùng **ntfy.sh** làm transport thay thế Telegram trên các máy bị chặn `api.telegram.org`. Setup phải **nhanh nhất có thể**: không bot, không token, không OAuth — chỉ cần 1 topic (auto-defined khi `init`).

## Motivation

User báo: "một số máy đã chặn tele rồi". Discord Bot vẫn cần token + OAuth + invite (15–20 phút). ntfy.sh:
- HTTP push thuần, không token
- App mobile native (iOS/Android), subscribe theo topic
- Self-host được nếu cần
- Topic auto-sinh tại `init` → user chỉ cần copy topic vào app điện thoại → xong

## Scope

### IN
- Add `NtfyClient` (HTTP push) song song `TelegramClient`
- Mở rộng `ChannelConfig` với field `transport` (telegram | ntfy) + ntfy-specific fields
- Branch các tool 1-chiều theo transport: `send_message`, `send_file`, `send_video`, `set_reminder`
- CLI: `mcgram channel add-ntfy`, `init` auto-sinh ntfy default channel với topic random
- `doctor` kiểm tra connectivity cho mỗi transport
- Skill SKILL.md cập nhật triggers

### OUT (deferred)
- `ask` 2-chiều trên ntfy (ntfy có Action buttons nhưng cần HTTP callback endpoint — phức tạp). Trả lỗi `transport_unsupported` rõ ràng.
- Discord/Slack/ntfy self-hosted auth (basic-auth header sẵn sàng nhưng không UI hỗ trợ trong phase này)

## Architecture decision

**KISS approach (chọn):** không tạo `Transport` protocol/abstraction tổng quát. Thay vì vậy:
- `ChannelConfig` chứa toàn bộ thông tin destination (transport + endpoint-specific fields)
- Mỗi tool tự branch `if cfg.transport == "ntfy": ... else: ...` (ngắn, ít chỉ một switch)
- `AppState` mang cả `tg_client` (optional) và `ntfy_client` (optional)

Lý do bỏ Transport protocol: chỉ có 2 backend, các tool khác nhau khá nhiều giữa 2 (button, callback_query không có ý nghĩa với ntfy), abstraction sẽ rò rỉ. YAGNI.

## Config schema mới

```yaml
bot:                              # optional nếu KHÔNG có channel telegram
  token_env: MCGRAM_BOT_TOKEN
  operator_chat_id: 123456789

ntfy:                             # optional nếu KHÔNG có channel ntfy
  server: https://ntfy.sh         # endpoint (mặc định)
  default_topic: mcgram-a1b2c3d4e5f6g7h8   # topic định sẵn khi init

channels:
  default:
    transport: ntfy               # ntfy | telegram (mặc định telegram nếu omit)
    ntfy_topic: mcgram-a1b2c3d4e5f6g7h8
    description: "Auto-created ntfy channel"

  team:                           # ví dụ thêm channel khác kiểu telegram
    transport: telegram
    chat_id: -1001234567890
```

**Quy tắc fallback:**
- Nếu `channel.transport == "ntfy"` mà thiếu `ntfy_topic` → lấy từ `ntfy.default_topic`
- Nếu `channel.transport == "telegram"` mà thiếu `chat_id` → lỗi config
- `default` channel:
  - Nếu `ntfy.default_topic` có → default = ntfy
  - Nếu `bot.operator_chat_id` có → default = telegram
  - Cả 2 → ưu tiên giữ behavior cũ (telegram), trừ khi user khai báo `channels.default.transport: ntfy`

## Topic policy (yêu cầu user)

Topic **MUST be defined** (không generate random ad-hoc khi gửi):
- `mcgram init` sinh 1 lần topic random `mcgram-<16-hex>` → ghi vào `~/.mcgram/config.yaml` dưới `ntfy.default_topic`
- User có thể edit thủ công nếu muốn topic dễ nhớ
- `mcgram channel add-ntfy <name> [--topic XXX]` — nếu omit `--topic` thì sinh random và ghi

Format topic: `mcgram-` prefix + 16 hex char (đủ entropy để chống đoán brute-force).

## Phase breakdown

| Phase | File | Mô tả | Tests |
|-------|------|-------|-------|
| 01 | [phase-01-ntfy-client.md](phase-01-ntfy-client.md) | `NtfyClient` HTTP wrapper (send_message, send_file, send_video, health_check) | unit |
| 02 | [phase-02-config-schema.md](phase-02-config-schema.md) | `ChannelConfig`, `NtfyConfig`, optional `BotConfig`, resolve_destination | unit |
| 03 | [phase-03-tools-branch.md](phase-03-tools-branch.md) | Branch `send_message/send_file/send_video` theo transport | unit + integration |
| 04 | [phase-04-ask-and-reminders.md](phase-04-ask-and-reminders.md) | `ask` graceful reject trên ntfy; reminders dùng send_message → auto work | unit |
| 05 | [phase-05-cli.md](phase-05-cli.md) | `channel add-ntfy`, `init` auto-topic, `doctor` per-transport, runtime guard khi không có telegram channel | unit |
| 06 | [phase-06-docs-skill.md](phase-06-docs-skill.md) | README, SKILL.md, config.example.yaml, docs/architecture.md | — |

## Files to modify/create

### New
- `src/mcgram/ntfy_client.py` — HTTP push client (~120 LOC)
- `tests/unit/test_ntfy_client.py`
- `tests/unit/test_config_ntfy.py`
- `tests/integration/test_tools_ntfy.py`

### Modify
- `src/mcgram/config.py` — schema mở rộng
- `src/mcgram/runtime.py` — `AppState` có thêm `ntfy_client: NtfyClient | None`
- `src/mcgram/server.py` — wire ntfy client khi cần, optional telegram polling
- `src/mcgram/tools/send_message.py`, `send_file.py`, `send_video.py` — branch
- `src/mcgram/tools/ask.py` — reject ntfy với lỗi rõ
- `src/mcgram/cli_init.py` — sinh topic random, write ntfy section vào config
- `src/mcgram/cli_doctor.py` — check per-transport
- `src/mcgram/cli_channel.py` — `add-ntfy` subcommand
- `src/mcgram/data/config.example.yaml` — thêm ví dụ ntfy
- `src/mcgram/data/skill/SKILL.md` — bổ sung note về ntfy + giới hạn ask
- `README.md` — section ntfy + quickstart so sánh

## Key risks

1. **Backward compat**: config cũ (chỉ có `bot:`) phải vẫn chạy → giữ default channel auto-create từ operator_chat_id khi không có ntfy.
2. **`ask` users surprise**: nếu user gọi ask trên ntfy channel sẽ fail. Mitigate: lỗi rõ "switch to a telegram channel for ask", và SKILL.md hướng dẫn Claude tránh.
3. **`send_file` size limit**: ntfy.sh free public có limit ~15 MB/file (giảm so với Telegram 50 MB). Update `file_max_bytes` heuristic theo transport.
4. **Topic guessable**: topic là URL công khai. Mitigate: 16-hex random (64-bit entropy) + tài liệu cảnh báo "đừng gửi data nhạy cảm trừ khi self-host ntfy".
5. **No auth header support phase này**: nếu user dùng ntfy có auth, phase 5 thêm `ntfy.access_token` field (nice-to-have, có thể defer).

## Success criteria

- [ ] `mcgram init` trên máy mới → có topic ntfy định sẵn trong config
- [ ] User subscribe topic trên ntfy mobile app → gửi `send_message` test → nhận push <2s
- [ ] `send_file` 5MB log file → nhận file trên mobile
- [ ] `mcgram doctor` báo OK cho ntfy transport
- [ ] Khi cấu hình chỉ có ntfy (không bot token), mcgram start được, không cần Telegram polling
- [ ] Khi cấu hình lai (cả Telegram + ntfy channels), 2 transport hoạt động độc lập
- [ ] `ask` trên ntfy channel trả lỗi `transport_unsupported` rõ ràng (không crash)
- [ ] Tests xanh: pytest, ruff, mypy (nếu có)
- [ ] Coverage ≥80%

## Open questions

1. **ntfy self-hosted auth (Bearer token)?** — Defer. Thêm `ntfy.access_token_env` ở phase 5 nếu cần.
2. **Push behavior khi ntfy server timeout?** — Retry 1 lần với backoff, sau đó audit error (giống Telegram TelegramError).
3. **Có nên migrate "default" channel sang ntfy nếu chỉ có ntfy config?** — Đề xuất: YES, để zero-config UX. Trừ khi user explicit set `channels.default.transport: telegram`.
4. **Có cần Discord transport tương lai không?** — Có thể, nhưng deferred. Architecture KISS hiện tại vẫn add được sau (chỉ thêm 1 branch nữa).
