# Phase 06 — Docs + Skill update

**Priority:** P1
**Status:** PENDING
**Depends on:** phase-01..05

## Overview

Cập nhật tài liệu để user và Claude biết:
- ntfy.sh là option mới
- Topic được defined ngay khi init
- `ask` chỉ chạy trên telegram
- Một số máy bị chặn Telegram → ntfy là fallback

## Files to update

### README.md
- Section **"Why ntfy.sh?"** (1 đoạn, ngắn) — lý do thêm transport
- **Quickstart split**: 3-bước ntfy (30 giây) vs 6-bước Telegram. Đặt ntfy LÊN TRÊN cho UX nhanh nhất.
- **The 7 tools table**: thêm cột "ntfy?" — ✅ cho send_*, set_reminder; ❌ cho ask
- **Config example**: dual block (ntfy / telegram)
- **Known limitations**: thêm "ntfy.sh: no 2-way ask; topic public-by-obscurity → use random topic or self-host"
- **Architecture**: update module list (`ntfy_client.py`)

### docs/architecture.md
- Thêm sơ đồ "two-transport dispatch": tool → resolve_destination → switch(transport) → ntfy_client | tg_client
- Bảng so sánh giới hạn 2 transport (file size, 2-way, auth model)

### src/mcgram/data/skill/SKILL.md
- Thêm trigger phrases tiếng Việt + Anh:
  - "gửi qua ntfy" / "notify via ntfy" / "push notification"
- **Quy tắc cho Claude**: nếu user nói "ask me", Claude phải verify channel transport là telegram trước; nếu chỉ có ntfy → nói rõ với user và đề xuất send_message thay thế
- Ví dụ:
  ```
  USER: "ping me when build done"
  → use send_message (works on any transport)

  USER: "ask me before deploying"
  → requires telegram channel; if config has only ntfy, tell user to set up bot first
  ```

### docs/security-threat-model.md
- Thêm threat: **T-NTFY-1** — topic guessable. Mitigation: 64-bit random topic; documentation cảnh báo không gửi PII; self-host option.
- **T-NTFY-2** — ntfy.sh server vận hành bởi bên thứ 3 (Heise Online / @binwiederhier). Mitigation: cho phép `ntfy.server` configurable → self-host.

### docs/images
- (Optional) Update `flow.png` để show 2 transport. Defer nếu mất thời gian — chỉ update text reference.

## CHANGELOG / version

- Bump version (semver minor) trong `pyproject.toml`: `0.X.0 → 0.(X+1).0`
- Add `CHANGELOG.md` entry nếu file tồn tại:
  ```
  ## [unreleased] — 2026-05-22
  ### Added
  - ntfy.sh transport for one-way notifications (channel `transport: ntfy`)
  - `mcgram channel add-ntfy <name>` CLI subcommand
  - `mcgram init` now generates a default ntfy topic
  - `mcgram doctor` checks both Telegram and ntfy transports independently
  ### Changed
  - `bot` config section is now optional (use either `bot` or `ntfy`)
  - Channels now declare `transport: telegram | ntfy`
  ### Limitations
  - `ask` tool requires a Telegram channel (no 2-way input on ntfy)
  ```

## Skill / pyproject entry

`pyproject.toml`:
- Keywords add `"ntfy"`, `"push-notifications"`
- Classifiers: no change
- Optional dep: no new dep required (`httpx` đã có)

## Acceptance

- README pass lint markdown
- SKILL.md syntactically valid (skill_installer load OK)
- Architecture doc lines updated với module mới
- Version bumped
