# Phase 03 — CLI `mcgram discord mention`

## Requirements

Lệnh quản lý mention map, ghi vào `discord.mentions` trong `config.yaml`.

## Files to modify

- `src/mcgram/cli_discord.py` (mới)
- `src/mcgram/cli.py` (dispatch `discord`)
- `src/mcgram/cli.py` `_HELP` (thêm dòng)
- `tests/test_cli_discord.py` (thêm class/test cho mention)

## Steps

### cli_discord.py (mirror cli_channel.py cho phần load/save/validate)

- Reuse `_config_path`, `_load_raw`, `_save_raw` pattern (copy nhỏ, KISS — hoặc import từ cli_channel nếu sạch; ưu tiên import `_config_path` không có → tự viết gọn).
- `_USER_ID_RE = re.compile(r"^\d{15,25}$")`.
- `cmd_mention_add(args)`: validate id qua regex; nếu fail → in lỗi + exit 2. `data.setdefault("discord", {})`; `discord.setdefault("mentions", {})[name] = user_id`; save; in `added  <name> -> <user_id>`.
- `cmd_mention_list(args)`: in bảng `name -> id` từ `discord.mentions`; rỗng → `(no mentions registered)`.
- `cmd_mention_remove(args)`: xoá key; không có → in lỗi + exit 1.
- `main(argv)`: argparse
  ```
  prog="mcgram discord"
  sub = add_subparsers(dest="group", required=True)
  mention = sub.add_parser("mention")
  msub = mention.add_subparsers(dest="action", required=True)
  add: name, user_id
  list: (none)
  remove: name
  ```
  dispatch theo (group, action).

### cli.py

- Trong `main()`: thêm
  ```python
  if first == "discord":
      from .cli_discord import main as discord_main
      sys.exit(discord_main(args[1:]))
  ```
- `_HELP`: thêm dòng `mcgram discord mention add NAME ID   register a @mention target (Discord)`.

## Tests

- `add` ghi đúng `discord.mentions.alice == "123..."`; tạo `discord:` nếu chưa có.
- id không hợp lệ (`abc`) → exit code 2, config không đổi.
- `list` in tên + id.
- `remove` xoá; remove tên không tồn tại → exit 1.
- Không phá config sẵn có (bot/ntfy/channels giữ nguyên sau save).

## Validation

`uv run pytest tests/test_cli_discord.py -q`
