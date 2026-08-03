# Phase 04 — Skill guidance + docs

## Requirements

1. Hướng dẫn AI viết Discord: ngắn gọn, đơn giản, trực tiếp, ≤2000 ký tự.
2. Tài liệu hoá tính năng mention (param + CLI).

## Files to modify

- `src/mcgram/data/skill/SKILL.md`
- `README.md`
- `src/mcgram/data/config.example.yaml`

## Steps

### SKILL.md — mục "Discord specifics"

- Thêm subsection **"Writing style for Discord"**:
  - Viết ngắn gọn, đơn giản, trực tiếp. Một ý chính mỗi message.
  - Hard limit **2000 ký tự** (không phải từ) — vượt trả `text_too_long`; tách message hoặc gửi file.
  - Không markdown nặng, không tường thuật dài; ưu tiên 1–3 dòng.
- Thêm subsection **"Mentions (@ping)"**:
  - Dùng `mention=["<tên đã đăng ký>"]` để ping. VD `send_message(text="deploy done", channel="eve", mention=["alice"])`.
  - Tên phải được đăng ký trước qua `mcgram discord mention add <tên> <user_id>`. Tên lạ → lỗi `unknown_mention` (đọc `known`).
  - KHÔNG tự viết `<@id>` trong text — chỉ registered user ping được; `@everyone`/`@here`/role luôn bị chặn.
  - `mention` trên telegram/ntfy bị bỏ qua (response có `note`).
- Cập nhật bảng tool ví dụ (thêm cột/ví dụ mention) và mục CLI (thêm `mcgram discord mention add`).
- Error recovery: thêm dòng `unknown_mention` → đăng ký tên trước hoặc kiểm tra `known`.

### README.md

- Mục "The 7 tools": note Discord `send_message/send_file/send_video` nhận optional `mention` (registered names).
- Mục CLI: thêm `mcgram discord mention list | add NAME ID | remove NAME`.
- Config example block: thêm `discord.mentions`.
- (tùy) một dòng ở phần Discord về mention + always-on allowed_mentions (bảo mật).

### config.example.yaml

- Trong block `# discord:` comment, thêm ví dụ:
  ```yaml
  #   mentions:                      # name -> Discord user id (for @ping via `mention=[...]`)
  #     alice: "123456789012345678"
  ```

## Validation

- `mcgram install-skill --force` không lỗi (skill hợp lệ).
- Đọc lại README/SKILL: dates/links/claims khớp thay đổi thực tế.
- Không có lệnh test (docs).
