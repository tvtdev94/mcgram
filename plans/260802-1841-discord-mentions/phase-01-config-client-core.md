# Phase 01 — Config + Discord client core

## Requirements

Đăng ký `name -> user id` ở config, đưa vào `Destination`, và luôn set `allowed_mentions` khi gửi Discord.

## Files to modify

- `src/mcgram/config.py`
- `src/mcgram/discord_client.py`
- `tests/test_config_discord.py`, `tests/test_discord_client.py`

## Steps

### config.py

1. `DiscordConfig`: thêm field
   ```python
   mentions: dict[str, str] = Field(default_factory=dict)
   ```
   Validator: mỗi value phải là chuỗi số (snowflake), key non-empty sau strip.
   ```python
   @field_validator("mentions")
   @classmethod
   def _check_mentions(cls, v):
       out = {}
       for name, uid in v.items():
           name = str(name).strip()
           if not name:
               raise ValueError("mention name must be non-empty")
           if not re.fullmatch(r"\d{15,25}", str(uid)):
               raise ValueError(f"mention {name!r}: user id must be a numeric Discord id")
           out[name] = str(uid)
       return out
   ```
   (thêm `import re`)

2. `Destination`: thêm
   ```python
   discord_mentions: dict[str, str] | None = None
   ```

3. `resolve_destination` nhánh discord: truyền `discord_mentions=dc.mentions` (dc = `self.discord or DiscordConfig()`).

### discord_client.py

4. Thêm 2 helper module-level:
   ```python
   def resolve_mentions(names, registry):
       ids, unknown = [], []
       for n in names:
           uid = registry.get(n)
           (unknown if uid is None else ids).append(n if uid is None else uid)
       return ids, unknown

   def format_mention_prefix(user_ids):
       return "".join(f"<@{uid}> " for uid in user_ids)
   ```

5. `_build_payload`: thêm param `mention_user_ids: list[str] | None = None`. Luôn set:
   ```python
   if mention_user_ids:
       payload["allowed_mentions"] = {"parse": [], "users": list(mention_user_ids)}
   else:
       payload["allowed_mentions"] = {"parse": []}
   ```
   (đặt trước `return payload`).

6. `send_message` và `send_file` (và `send_video` qua delegate): thêm param `mention_user_ids: list[str] | None = None`, truyền vào `_build_payload`. Client KHÔNG tự prepend `<@id>` — content đến từ tool đã gồm prefix.

## Tests

- config: `discord.mentions` parse OK; value non-numeric → ConfigError; `Destination.discord_mentions` populated; channel không có `discord:` → mentions `{}`.
- client: không mention → payload có `allowed_mentions={"parse":[]}`; có `mention_user_ids=["1","2"]` → `{"parse":[],"users":["1","2"]}`; `format_mention_prefix(["1"]) == "<@1> "`; `resolve_mentions` trả unknown đúng.
- Regression: các test client cũ vẫn pass (assert theo key, additive).

## Validation

`uv run pytest tests/test_config_discord.py tests/test_discord_client.py -q`
