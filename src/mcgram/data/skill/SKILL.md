---
name: mcgram
description: Notification bridge for Claude Code (Telegram + ntfy.sh) — send notifications, ask short questions, set reminders via the user's personal bot or ntfy topic.
when_to_use: The user wants to be notified (Telegram or ntfy.sh) when a task finishes, wants to be asked a short approve/reject/pick question before a risky action, wants to be reminded after N minutes, or asks to send a file (log, screenshot, artifact) to their phone.
---

# mcgram — notification bridge (Telegram + ntfy.sh)

Use this skill when the user says: "báo telegram khi xong", "tell me on Telegram when done",
"gửi qua ntfy", "push to my phone", "nhắc tôi", "remind me in X minutes",
"hỏi tôi confirm trước khi", "ask me before deploying",
"send log", "gửi file qua telegram/ntfy".

## Tools

| User intent | Tool | Example |
|---|---|---|
| "Báo khi xong" / "ping me when done" | `send_message` | `send_message(text="✅ Build passed in 3m12s")` |
| "Gửi log lỗi" / "send the failing log" | `send_file` | `send_file(path="./logs/test-failures.log", caption="Failing tests")` |
| "Gửi video" / "send the demo recording" | `send_video` | `send_video(path="./demo.mp4", caption="Feature demo")` — inline-plays |
| "Confirm trước khi deploy" / "ask me before X" | `ask` with 2 options | `ask(question="Deploy to prod?", options=["Approve","Cancel"], timeout_s=300)` |
| "Chọn giúp tôi" / "pick one" | `ask` with options | `ask(question="Which DB to migrate?", options=["staging","prod"])` |
| "Hỏi tôi tự do" / "open-ended question" | `ask` without options | `ask(question="Any special instructions?")` |
| "Nhắc sau 30 phút" / "remind me in 30 min" | `set_reminder` | `set_reminder(text="Check container logs", delay_s=1800)` |
| "Hủy lời nhắc" / "cancel reminder" | `cancel_reminder` | `cancel_reminder(reminder_id="r_abc")` |
| "Xem các nhắc hiện tại" / "list reminders" | `list_reminders` | `list_reminders()` |

## Transport awareness (Telegram vs ntfy.sh)

The user may configure **Telegram**, **ntfy.sh**, or **both**. Each channel declares its transport. Most tools work on either, but `ask` is **Telegram-only** (ntfy.sh has no 2-way input).

| Tool | Telegram | ntfy.sh |
|---|---|---|
| `send_message`, `send_file`, `send_video` | ✅ | ✅ |
| `set_reminder`, `cancel_reminder`, `list_reminders` | ✅ | ✅ |
| `ask` | ✅ | ❌ returns `transport_unsupported` |

If `ask` returns `error: "transport_unsupported"`, **do not retry**. Tell the user:
"This channel uses ntfy.sh which doesn't support 2-way input. I'll send a notification instead and proceed with a safe default — or switch to a telegram channel if you want me to wait for your reply."

Each tool response includes a `transport` field (`"telegram"` or `"ntfy"`) so you know what got used.

## Channels (routing to different chats/groups)

The user can configure named **channels** in `~/.mcgram/config.yaml` — each maps to a Telegram chat OR a ntfy topic. Every send-style tool accepts an optional `channel` parameter:

```python
send_message(text="🚨 prod down", channel="oncall")       # → oncall destination
send_message(text="release notes ...", channel="team")    # → team chat
send_message(text="…")                                    # → 'default'
ask(question="Approve?", options=["Yes","No"], channel="oncall")  # Telegram required
set_reminder(text="rotate keys", delay_s=3600, channel="security")
```

**When to use which channel:**

- **Default**: omit `channel` → goes to the default destination (operator chat OR default ntfy topic).
- **User says "gửi vào kênh X" / "post to #X"**: pass `channel="X"`.
- **User mentions a team / role**: translate to the matching channel name. If unsure, ask first.
- **Error `unknown_channel`**: the name isn't in the config. Tell the user to add it.

**The user manages channels with the CLI:**

```bash
mcgram channel list                                          # show all channels + transports
mcgram channel add oncall -1001234567890 -d "Pager"          # Telegram channel
mcgram channel add-ntfy alerts --topic mcgram-x9k2 -d "..."  # ntfy channel (random topic if --topic omitted)
mcgram channel remove oncall
```

**Don't:** invent channel names. Either use `default` or ask.

## Defaults & limits

- `ask` timeout default 120s, max 600s. **`ask` BLOCKS the Claude Code session** until reply or timeout — keep timeouts short.
- `send_file` size cap **50 MB** for Telegram. ntfy.sh public free tier ~15 MB; self-hosted may be higher. If a large file fails on ntfy, suggest the user split it or self-host.
- Caption max 1024 chars (Telegram limit) — auto-truncated with ellipsis.
- Max **10 pending reminders**. Max delay **24h**. Reminders **lost on Claude Code shutdown** — don't promise long-term reminders.
- Max **6 buttons** per `ask`. Use freetext (no `options`) for open-ended answers.
- Rate limit: 20 calls per minute per tool.

## Error recovery

- `rate_limit_exceeded` → wait 60s, retry.
- `transport_unsupported` (from `ask` on a ntfy channel) → do **NOT** retry. Send a notification with `send_message` and proceed with a safe default, OR ask the user to switch to a telegram channel.
- `transport_unavailable` → the channel's transport client isn't initialized (e.g. ntfy channel but no `ntfy` section in config). Tell the user to fix config.
- `path_outside_cwd` → only files inside CWD are sendable. Ask the user to enable `allow_outside_cwd`, or move the file.
- `file_too_large` → file exceeds the size cap. Compress, split, or summarize.
- `ask` returning `source: "timeout"` → user didn't reply. Decide: retry, default, or abort.
- `text_too_long` → text exceeds 4096 chars; split or send as a file.
- `reminder_max_pending` → too many pending reminders. Cancel some via `cancel_reminder`.
- `telegram_api` / `ntfy_api` → transport error from the upstream service. Read the `reason` field for details.

## How `ask` resolves (Telegram only)

User replies by **tapping a button** (when `options` provided) or by **typing a free-text reply**. First reply wins. Timeout → `{source: "timeout", value: ""}`.

Source field tells you HOW they replied:
- `button` — tapped one of the provided options (value = button label)
- `freetext` — typed a custom reply (value = their text)
- `timeout` — no reply within the deadline

## Audit

Every tool call is logged to `~/.mcgram/audit.jsonl` with the `transport` field. Inspect with `mcgram audit`. For sensitive payloads, suggest the user enable `redact_text: true` in `~/.mcgram/config.yaml`.

## Don't do

- Don't send raw secrets, passwords, or production credentials.
- For **ntfy.sh public topics**: assume **anyone with the topic URL can read your messages**. Don't send personally identifiable info or secrets unless the user is on a self-hosted ntfy with auth.
- Don't schedule reminders longer than 24h — they're in-memory and lost on restart.
- Don't promise reminders will survive Claude Code shutdown.
- Don't retry `ask` on a ntfy channel after `transport_unsupported`.
- Don't use very long `ask` timeouts (>5 min) — they freeze the Claude Code session.
- Don't spam — respect the 20/min rate limit. Batch updates.

## Example good interactions

**User**: "Run the tests then báo cho tôi qua telegram khi xong."
**You**:
1. Run the test suite locally.
2. On completion: `send_message(text="✅ 142 tests passed in 4m23s")` or `send_message(text="❌ 3 failures, see attached log")` + `send_file(path="./test-failures.log")`.

**User**: "Tôi đi ăn trưa, nhắc tôi quay lại check deploy sau 45 phút."
**You**: `set_reminder(text="Check production deploy status", delay_s=2700)` → reply with the reminder_id.

**User**: "Trước khi tôi merge PR này, hỏi confirm trên Telegram."
**You**: `ask(question="Merge PR #142 to main?", options=["Approve","Cancel"], timeout_s=300)` → branch on `value`. If `ask` returns `transport_unsupported`, fall back to `send_message` and ask the user how to proceed.

**User**: "Push lên ntfy đi, máy này chặn telegram rồi."
**You**: Use `send_message(text="...", channel="default")` (assumes default is ntfy). If they want a separate channel, ask which name to use or suggest `mcgram channel add-ntfy NAME` first.
