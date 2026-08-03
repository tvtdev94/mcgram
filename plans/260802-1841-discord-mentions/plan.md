# Discord mentions + concise-writing skill guidance

**Status:** DONE — all 4 phases implemented, 83 discord/mention tests pass, code-review DONE (no blockers)
**Branch:** main
**Date:** 2026-08-02

## Goal

1. Cho phép đăng ký `name -> Discord user id` và mention (`@ping`) user khi gửi message/file/video vào Discord.
2. Luôn set `allowed_mentions` trên MỌI message Discord → chỉ user được chỉ định mới ping; chặn `@everyone`/`@here`/role kể cả khi lọt vào text.
3. Bổ sung hướng dẫn viết Discord (ngắn gọn, đơn giản, trực tiếp, ≤2000 ký tự) vào SKILL.md.

## Verified facts (websearch)

- Discord `content` = **2000 ký tự** (đã enforce sẵn ở `send_message.py`).
- Mention user = `<@USER_ID>`.
- Webhook KHÔNG ping nếu thiếu `allowed_mentions`. An toàn: `{"parse": [], "users": ["id1"]}` (không đặt `"users"` trong `parse` đồng thời có mảng `users`).

## Design (đã chốt với user)

- **Đăng ký:** CLI `mcgram discord mention add|list|remove`, lưu vào `discord.mentions` trong `config.yaml` (user id không phải secret → không dùng `.env`).
- **Trigger:** param `mention: list[str]` (tên đã đăng ký) trên `send_message`/`send_file`/`send_video`. Tên lạ → lỗi `unknown_mention` liệt kê tên hợp lệ. Non-Discord → bỏ qua kèm `note`.
- **Phạm vi:** chỉ user mention. `allowed_mentions` luôn được set.

## Phases

| Phase | Nội dung | Files | Depends |
|---|---|---|---|
| 01 | Config + client core: `DiscordConfig.mentions`, `Destination.discord_mentions`, always-on `allowed_mentions`, helpers resolve/format | `config.py`, `discord_client.py` + tests | — |
| 02 | Dispatch + tools: thread `mention_user_ids`, param `mention`, build content, length-check gồm prefix, audit | `dispatch.py`, `tools/send_message.py`, `tools/send_file.py`, `tools/send_video.py` + tests | 01 |
| 03 | CLI `mcgram discord mention add/list/remove` | `cli.py`, `cli_discord.py` (mới) + tests | 01 |
| 04 | Skill + docs: SKILL.md (writing guidelines + mention), README, config.example.yaml | `data/skill/SKILL.md`, `README.md`, `data/config.example.yaml` | 02,03 |

## Acceptance criteria

- `send_message(text="deploy done", channel="eve", mention=["alice"])` → content bắt đầu `<@ALICE_ID> …`, payload có `allowed_mentions={"parse":[],"users":["ALICE_ID"]}`, Discord ping alice.
- `mention=["ghost"]` (chưa đăng ký) → `{"error":"invalid_input","reason":"unknown_mention","unknown":["ghost"],"known":[...]}`, không gọi network.
- Message Discord không mention → payload vẫn có `allowed_mentions={"parse":[]}` (chặn @everyone).
- `mention` trên telegram/ntfy → gửi bình thường + `note` "mention ignored".
- Length-check tính cả prefix mention (text 1995 ký tự + 1 mention vượt 2000 → `text_too_long`).
- `mcgram discord mention add alice 123456789012345678` ghi `discord.mentions.alice`; `list` in ra; `remove` xoá.
- Token webhook KHÔNG bao giờ xuất hiện trong audit (giữ nguyên bảo đảm cũ).
- Toàn bộ suite pass, ruff sạch, mypy không phát sinh lỗi mới.

## Scope boundary (OUT)

- Không role mention, không `@everyone`.
- Không per-channel default mentions.
- Không đổi transport Telegram/ntfy (mention chỉ no-op + note).
- Không sửa `mcgram doctor` (không cần hiển thị mentions).

## Risks / rollback

- **Behavior change:** always-on `allowed_mentions`. User đã duyệt (mục tiêu bảo mật). Rollback = bỏ nhánh set khi `mention_user_ids` rỗng.
- Tests assert exact payload shape có thể cần cập nhật (đa số assert theo key, ít rủi ro).
