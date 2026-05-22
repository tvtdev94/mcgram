---
name: mcgram
description: Telegram bridge for Claude Code — send notifications, ask short questions, set reminders via the user's personal Telegram bot.
when_to_use: The user wants to be notified on Telegram when a task finishes, wants to be asked a short approve/reject/pick question before a risky action, wants to be reminded after N minutes, or asks to send a file (log, screenshot, artifact) to their Telegram.
---

# mcgram — Telegram bridge

Use this skill when the user says: "báo telegram khi xong", "tell me on Telegram when done",
"nhắc tôi", "remind me in X minutes", "hỏi tôi confirm trước khi", "ask me before deploying",
"send log to Telegram", "gửi file qua Telegram".

## Tools

| User intent | Tool | Example |
|---|---|---|
| "Báo khi xong" / "ping me when done" | `send_message` | `send_message(text="✅ Build passed in 3m12s")` |
| "Gửi log lỗi" / "send the failing log" | `send_file` | `send_file(path="./logs/test-failures.log", caption="Failing tests")` |
| "Gửi video" / "send the demo recording" | `send_video` | `send_video(path="./demo.mp4", caption="Feature demo")` — plays in-chat |
| "Confirm trước khi deploy" / "ask me before X" | `ask` with 2 options | `ask(question="Deploy to prod?", options=["Approve","Cancel"], timeout_s=300)` |
| "Chọn giúp tôi" / "pick one" | `ask` with options | `ask(question="Which DB to migrate?", options=["staging","prod"])` |
| "Hỏi tôi tự do" / "open-ended question" | `ask` without options | `ask(question="Any special instructions?")` |
| "Nhắc sau 30 phút" / "remind me in 30 min" | `set_reminder` | `set_reminder(text="Check container logs", delay_s=1800)` |
| "Hủy lời nhắc" / "cancel reminder" | `cancel_reminder` | `cancel_reminder(reminder_id="r_abc")` |
| "Xem các nhắc hiện tại" / "list reminders" | `list_reminders` | `list_reminders()` |

## Channels (routing to different chats/groups)

The user can configure named **channels** in `~/.mcgram/config.yaml` — each maps to a different Telegram chat (DM, group, or topic). Every send-style tool accepts an optional `channel` parameter:

```python
send_message(text="🚨 prod down", channel="oncall")       # → oncall group
send_message(text="release notes ...", channel="team")    # → team chat
send_message(text="…")                                    # → 'default' (the operator)
ask(question="Approve?", options=["Yes","No"], channel="oncall")
set_reminder(text="rotate keys", delay_s=3600, channel="security")
```

**When to use which channel:**

- **Default behavior**: omit `channel` → goes to the operator's personal chat (the user themselves).
- **User says "gửi vào kênh X" / "post to #X channel"**: pass `channel="X"`.
- **User mentions a team / group / role** ("ping the oncall", "tell the bugs group"): translate to the matching channel name. If unsure which configured name fits, **ask the user** which channel to use (or call `send_message` with no `channel` first, mentioning you sent to default).
- **Error `unknown_channel`**: the name isn't in the config. Tell the user to run `mcgram channel add NAME CHAT_ID` to add it, or list available channels.

**The user manages channels with the CLI:**

```bash
mcgram channel list                                # show all channels
mcgram channel add oncall -1001234567890 -d "Pager rotation"
mcgram channel remove oncall
```

**Don't:** invent channel names. If the user hasn't configured one, the call will fail with `unknown_channel`. Either use `default` or ask first.

## Defaults & limits

- `ask` timeout default 120s, max 600s. **`ask` BLOCKS the Claude Code session** until reply or timeout — keep timeouts short.
- `send_file` size cap **50 MB**. Paths must be inside CWD unless config sets `allow_outside_cwd: true`.
- Caption max 1024 chars (Telegram limit) — auto-truncated with ellipsis.
- Max **10 pending reminders**. Max delay **24h**. Reminders **lost on Claude Code shutdown** — don't promise long-term reminders.
- Max **6 buttons** per `ask`. Use freetext (no `options`) for open-ended answers.
- Rate limit: 20 calls per minute per tool.

## Error recovery

- `rate_limit_exceeded` → wait 60s, retry.
- `path_outside_cwd` → only files inside the current working directory are sendable by default. Ask the user to enable `allow_outside_cwd` in config, or move the file into CWD.
- `file_too_large` → file exceeds 50 MB. Compress, split, or summarize before sending.
- `ask` returning `source: "timeout"` → user didn't reply. Decide: retry, fall back to a default, or abort. Tell the user the question timed out.
- `text_too_long` → Telegram caps text at 4096 chars; split into multiple messages or send as a file.
- `reminder_max_pending` → too many pending reminders. Use `cancel_reminder` or `list_reminders` to free a slot.

## How `ask` resolves

The user can reply by **tapping a button** (when `options` provided) or by **typing a free-text reply** in the same chat. The first reply wins. If they don't reply within `timeout_s`, the tool returns `{source: "timeout", value: ""}`.

The response includes a `source` field telling you HOW they replied:
- `button` — tapped one of the provided options (value = button label)
- `freetext` — typed a custom reply (value = their text)
- `timeout` — no reply within the deadline

## Audit

Every tool call is logged to `~/.mcgram/audit.jsonl`. User can inspect with `mcgram audit`. The audit log includes message text by default — if the user mentions sensitive payloads, suggest they enable `redact_text: true` in `~/.mcgram/config.yaml`.

## Don't do

- Don't send raw secrets, passwords, or production credentials in `send_message` or `send_file`.
- Don't schedule reminders longer than 24h — they're in-memory and lost on restart.
- Don't promise reminders will survive Claude Code shutdown.
- Don't use very long `ask` timeouts (>5 min) — they freeze the Claude Code session.
- Don't spam — respect the 20/min rate limit. If you have many updates to send, batch them.

## Example good interactions

**User**: "Run the tests then báo cho tôi qua Telegram khi xong."
**You**:
1. Run the test suite locally.
2. On completion: `send_message(text="✅ 142 tests passed in 4m23s")` or `send_message(text="❌ 3 failures, see attached log")` + `send_file(path="./test-failures.log")`.

**User**: "Tôi đi ăn trưa, nhắc tôi quay lại check deploy sau 45 phút."
**You**: `set_reminder(text="Check production deploy status", delay_s=2700)` → reply with the reminder_id.

**User**: "Trước khi tôi merge PR này, hỏi confirm trên Telegram."
**You**: `ask(question="Merge PR #142 to main?", options=["Approve","Cancel"], timeout_s=300)` → branch on `value`.
