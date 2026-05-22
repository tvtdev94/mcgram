# Phase 05 — CLI: init topic, channel add-ntfy, doctor per-transport

**Priority:** P0 — đây là điểm chạm UX user yêu cầu "setup nhanh nhất"
**Status:** PENDING
**Depends on:** phase-02

## Overview

3 thay đổi CLI:
1. `mcgram init` → sinh topic ngẫu nhiên, ghi `ntfy.default_topic` vào config khi config được scaffold lần đầu
2. `mcgram channel add-ntfy <name> [--topic TOPIC] [--server URL]` → add channel kiểu ntfy
3. `mcgram doctor` → kiểm tra connectivity cho cả Telegram (nếu có bot) và ntfy (nếu có)

## init flow mới

```python
def _generate_topic() -> str:
    return f"mcgram-{secrets.token_hex(8)}"  # 16 hex chars → 64-bit entropy

def init_config(*, force: bool = False) -> int:
    # ... existing scaffold ...
    # When writing config.yaml for the first time (or --force):
    #   substitute placeholder `{{NTFY_TOPIC}}` in bundled config.example.yaml
    topic = _generate_topic()
    template = _bundled_config_yaml().replace("{{NTFY_TOPIC}}", topic)
    cfg.write_text(template, encoding="utf-8")
    # ... print next steps including ntfy quickstart ...
```

### Next-steps UX mới (in từ `init`)

```
Next steps — choose ONE transport:

  [A] ntfy.sh (fastest, 30 seconds, no token):
    1. Install ntfy app on your phone (iOS / Android / Web)
    2. Subscribe to topic:  mcgram-<your-topic>
    3. Test:  mcgram doctor
    4. Restart Claude Code → /mcp → mcgram appears

  [B] Telegram (2-way ask supported):
    1. @BotFather → /newbot → copy token
    2. Edit ~/.mcgram/.env       → MCGRAM_BOT_TOKEN=...
    3. Edit ~/.mcgram/config.yaml → uncomment bot section, set operator_chat_id
    4. Test:  mcgram doctor

You can use BOTH — declare multiple channels with different transports.
```

## config.example.yaml (template) — kết hợp 2 transport, comment-out

```yaml
# mcgram config — copy to ~/.mcgram/config.yaml

# === Choose at least one transport ===

# Option A — ntfy.sh (no token, no bot, just a topic)
ntfy:
  server: https://ntfy.sh
  default_topic: {{NTFY_TOPIC}}    # subscribe to this topic in the ntfy mobile app
  # access_token_env: NTFY_TOKEN   # only for ntfy paid/self-hosted with auth

# Option B — Telegram (2-way `ask` supported)
# bot:
#   token_env: MCGRAM_BOT_TOKEN
#   operator_chat_id: 0            # your personal chat ID

defaults:
  parse_mode: plain
  ask_timeout_s: 120
  rate_limit_per_min: 20

# ... limits, audit, channels (commented examples) ...
```

## channel add-ntfy subcommand

```python
add_ntfy = sub.add_parser("add-ntfy", help="add a ntfy.sh channel")
add_ntfy.add_argument("name")
add_ntfy.add_argument("--topic", help="explicit topic; random if omitted")
add_ntfy.add_argument("--server", help="ntfy server URL (defaults to ntfy.default_topic config)")
add_ntfy.add_argument("--description", "-d")
add_ntfy.add_argument("--force", action="store_true")
```

Behavior:
- Topic random nếu omit: `mcgram-{8-hex}`
- Ghi vào config dạng `channels.<name>: { transport: ntfy, ntfy_topic: ... }`
- Print: `added <name>  topic=mcgram-xxx  → subscribe: <server>/<topic>`

`channel list` cập nhật để show transport:
```
  default  ntfy      topic=mcgram-a1b2c3d4...   Auto-created
  team     telegram  chat_id=-1001234567890    Team chat
```

## doctor flow mới

```python
async def _run_checks(settings):
    rows = []; failures = 0
    if settings.bot:
        # existing telegram checks
        ...
    if settings.ntfy:
        async with NtfyClient(settings.ntfy.server, access_token=...) as nc:
            try:
                if await nc.health():
                    rows.append(_ok("ntfy health", settings.ntfy.server))
                else:
                    rows.append(_fail("ntfy health", "non-200"))
                    failures += 1
            except NtfyError as e:
                rows.append(_fail("ntfy health", str(e)))
                failures += 1
            # send test message to default topic
            try:
                await nc.send_message(
                    settings.ntfy.default_topic,
                    "🩺 mcgram doctor: ntfy connection OK",
                    title="mcgram doctor",
                )
                rows.append(_ok("ntfy send test", f"topic={settings.ntfy.default_topic}"))
            except NtfyError as e:
                rows.append(_fail("ntfy send test", str(e)))
                failures += 1
    if not settings.bot and not settings.ntfy:
        rows.append(_fail("no transport configured", "set bot or ntfy in config"))
        failures += 1
    return failures, rows
```

## runtime.py / server.py changes

`_run()` start logic:
- Khởi tạo `tg_client` **CHỈ KHI** `settings.bot` is not None
- Khởi tạo `ntfy_client` **CHỈ KHI** `settings.ntfy` is not None
- Polling task **CHỈ START** khi có `tg_client` (ntfy không cần polling)
- Lock check như cũ (vẫn hợp lệ kể cả ntfy-only)
- `_wire_phase3`: chỉ wire `ask_registry` nếu có tg_client (ngược lại `ask_registry` = None → ask tool tự reject ở phase 4)

## Files

- `src/mcgram/cli_init.py` — modify (`_generate_topic`, substitute template)
- `src/mcgram/cli_channel.py` — modify (`cmd_add_ntfy`, show transport in list)
- `src/mcgram/cli_doctor.py` — modify (per-transport checks)
- `src/mcgram/data/config.example.yaml` — modify (placeholder)
- `src/mcgram/server.py` — modify (conditional client/polling start)
- `src/mcgram/runtime.py` — modify (`tg_client | None`, `ntfy_client | None`)

## Tests

- [ ] `test_init_generates_unique_topic_per_call` (run init twice → 2 different topics)
- [ ] `test_init_preserves_existing_config_without_force`
- [ ] `test_init_writes_topic_into_yaml`
- [ ] `test_channel_add_ntfy_with_explicit_topic`
- [ ] `test_channel_add_ntfy_with_random_topic`
- [ ] `test_channel_list_shows_transport_column`
- [ ] `test_doctor_passes_with_ntfy_only_config` (mock NtfyClient)
- [ ] `test_doctor_passes_with_telegram_only_config` (regression)
- [ ] `test_doctor_passes_with_hybrid_config`
- [ ] `test_doctor_fails_with_neither_transport_configured`
- [ ] `test_server_starts_without_polling_when_ntfy_only`

## Acceptance

- `mcgram init` trên hệ thống mới → config có ntfy section + topic + lệnh user-friendly
- `mcgram doctor` PASS với ntfy-only setup khi topic subscribe + test send thành công
- `mcgram channel list` hiển thị transport
- Telegram-only setup (regression) vẫn pass nguyên si

## Risk

- **`init` ghi đè topic khi user đã chỉnh tay?** → KHÔNG, chỉ ghi khi `cfg` chưa tồn tại HOẶC `--force`. Idempotent.
- **Polling-less mode**: chú ý lock check vẫn cần (tránh 2 instance ghi cùng audit.jsonl). OK với lock hiện tại.
