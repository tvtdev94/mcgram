---
phase: 2
title: Telegram Client & Send Tools
status: completed
priority: P2
effort: 1d
dependencies:
  - 1
---

# Phase 2: Telegram Client & Send Tools

## Overview

Build the Telegram Bot API client (async httpx wrapper + long-poll loop), MCP stdio server entry, rate limiter, and the two outbound tools: `send_message` and `send_file`. After this phase Claude Code can post text + attachments to the operator chat.

## Context Links

- Brainstorm: [`../20260521-brainstorm-mcgram-telegram-mcp/brainstorm-report.md`](../20260521-brainstorm-mcgram-telegram-mcp/brainstorm-report.md) §4 (Architecture), §5 (Security)
- Telegram Bot API: https://core.telegram.org/bots/api
- Reference: `C:\w\dbread\src\dbread\server.py` for MCP stdio pattern, `C:\w\dbread\src\dbread\rate_limiter.py` for token bucket

## Requirements

**Functional**
- `TelegramClient` async methods: `get_me`, `send_message`, `send_document`, `get_updates` (long-poll), `answer_callback_query`, `edit_message_reply_markup`
- Polling loop: `getUpdates` with `offset` + `timeout=25`, dispatch each update to registered handlers, skip updates not from `operator_chat_id`
- `RateLimiter` per-tool token bucket (default 20/min) + global cap; reject with `RateLimitError`
- Tool `send_message(text: str, silent: bool = False, parse_mode: str | None = None) -> {message_id, ok}`
- Tool `send_file(path: str, caption: str | None = None, silent: bool = False) -> {message_id, ok}`
- `send_file` guards: path resolved absolute, must be regular file, size ≤ `limits.file_max_bytes`, no path traversal beyond CWD unless `allow_outside_cwd: true` in config (default false)
- Polling task started on MCP `initialize`, cancelled on shutdown

**Non-functional**
- Polling loop survives transient network errors (exponential backoff, max 30s)
- All modules < 200 LOC
- No blocking I/O on event loop

## Architecture

```
src/mcgram/
├── server.py           # MCP stdio entry; register tools; start/stop polling task
├── tg_client.py        # httpx async wrapper (~150 LOC)
├── update_dispatcher.py # routes incoming updates → handler list
├── rate_limiter.py     # token bucket, per-tool + global (~80 LOC)
├── tools/
│   ├── __init__.py
│   ├── send_message.py # tool handler
│   └── send_file.py    # tool handler
└── runtime.py          # AppState: client, dispatcher, rate_limiter, audit, ask_registry (later), reminders (later)
```

### Polling loop pseudo-code

```python
async def poll_loop(client, dispatcher, operator_chat_id):
    offset = 0
    backoff = 1
    while True:
        try:
            updates = await client.get_updates(offset=offset, timeout=25)
            backoff = 1
            for upd in updates:
                offset = upd["update_id"] + 1
                if not _from_operator(upd, operator_chat_id):
                    audit.write({"tool": "_polling", "status": "rejected", "reason": "non_operator"})
                    continue
                await dispatcher.dispatch(upd)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            audit.write({"tool": "_polling", "status": "error", "error": str(e)})
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
```

### MCP server bootstrap

```python
async def main():
    settings = Settings.load()
    lock = SingleInstanceLock(settings.lock_path)  # from Phase 1
    with lock:
        async with TelegramClient(settings) as client:
            audit = AuditLog(settings.audit)
            rate = RateLimiter(settings.defaults.rate_limit_per_min)
            dispatcher = UpdateDispatcher()
            state = AppState(client, dispatcher, rate, audit, settings)
            poll_task = asyncio.create_task(poll_loop(...))
            try:
                await mcp_stdio_serve(register_tools(state))
            finally:
                poll_task.cancel()
                with suppress(asyncio.CancelledError):
                    await poll_task
```

## Related Code Files

- **Create:**
  - `src/mcgram/server.py`
  - `src/mcgram/runtime.py`
  - `src/mcgram/tg_client.py`
  - `src/mcgram/update_dispatcher.py`
  - `src/mcgram/rate_limiter.py`
  - `src/mcgram/tools/__init__.py`
  - `src/mcgram/tools/send_message.py`
  - `src/mcgram/tools/send_file.py`
- **Modify:**
  - `src/mcgram/__init__.py` (export `__version__`)
  - `pyproject.toml` (verify `mcp` SDK version)

## Implementation Steps

1. **tg_client.py** — async context manager wrapping `httpx.AsyncClient`. Base URL `https://api.telegram.org/bot{token}`. Methods raise `TelegramError(status, description)` on non-200/`ok=False`. `get_updates` uses `read_timeout=30` to outlast 25s long-poll.
2. **rate_limiter.py** — token bucket, refill `rate_per_min/60` tokens per second, capacity = `rate_per_min`. `try_acquire(tool: str) -> bool`. Per-tool buckets via dict.
3. **update_dispatcher.py** — `register(handler: Callable[[dict], Awaitable[None]])` list. `dispatch(update)` calls each handler; handlers are responsible for matching their own update type (callback_query, message, etc.).
4. **runtime.py** — `AppState` dataclass holding shared deps (client, audit, rate, settings, ask_registry placeholder, reminders placeholder).
5. **tools/send_message.py** — input validation, rate check, `client.send_message(chat_id, text, parse_mode, disable_notification=silent)`, audit log {tool, status, text_len, ms}, return `{message_id}`.
6. **tools/send_file.py** — path resolve → `Path(path).resolve()`. Reject if not exists / not file / size > cap. If `allow_outside_cwd` false and path not within CWD → reject. Open binary, `client.send_document(chat_id, file, caption)`. Audit {tool, status, bytes, ms}.
7. **server.py** — MCP stdio entry. Register `send_message` + `send_file` as tools with JSON schemas. Start poll_loop task. On shutdown cancel cleanly.
8. **Manual smoke test** — set test bot token + chat_id, run server, drive via raw stdio JSON-RPC, confirm message arrives in Telegram.

## Success Criteria

- [ ] `mcgram` runs via `uv run mcgram`; MCP `initialize` succeeds
- [ ] Sample JSON-RPC `tools/list` returns `send_message`, `send_file`
- [ ] `send_message("hello")` delivers message; response contains `message_id`
- [ ] `send_file` with valid 1 KB file delivers attachment
- [ ] `send_file` with 100 MB file rejected with `file_too_large`
- [ ] `send_file` with `..\..\secret.txt` rejected with `path_outside_cwd`
- [ ] Rate-limiting: 21st call within 60s rejected with `rate_limit_exceeded`
- [ ] Non-operator update logged with `status: rejected, reason: non_operator`, NOT processed
- [ ] Polling survives `kill -STOP`/network blip — backoff retries, no crash
- [ ] All new modules < 200 LOC

## Risk Assessment

- **httpx event-loop policy on Windows**: ProactorEventLoopPolicy needed for asyncio subprocess; OK for httpx. Verify Windows CI.
- **Telegram 409 Conflict** if another process polls same bot → caught + clear error message ("another mcgram already running OR token in use elsewhere").
- **Long-poll holds 25s connection** — fine; httpx `read_timeout=30`.
- **send_document streaming**: large file uses multipart; httpx handles. Verify ≥10 MB upload works in smoke.

## Security Considerations

- **Operator allowlist enforced at update_dispatcher entry** — non-operator updates never reach tool handlers
- **Token never logged** — audit records `chat_id` only
- **send_file path traversal**: default cap = CWD; opt-in `allow_outside_cwd`
- **Caption length cap 1024** (Telegram limit) — truncate with ellipsis

## Next Steps

Phase 3 — interactive `ask` flow + reminders use this client + dispatcher.
