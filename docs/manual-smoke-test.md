# Manual smoke test checklist

Run before tagging a release. Requires: a real Telegram bot token + your chat ID. Takes ~5 minutes.

## Setup

```bash
uv tool install --reinstall .              # or pipx install --force .
mcgram init                                 # scaffold ~/.mcgram/
echo "MCGRAM_BOT_TOKEN=<your-token>" > ~/.mcgram/.env
# Edit ~/.mcgram/config.yaml → set operator_chat_id
```

## Checks

- [ ] `mcgram --version` → prints `mcgram 0.1.0`
- [ ] `mcgram --help` → lists init / doctor / audit / install-skill
- [ ] `mcgram init` re-run → reports `skipped (already exists)`
- [ ] `mcgram init --force` → re-writes `config.yaml`
- [ ] `mcgram doctor` → all checks `[OK]`, you receive `🩺 mcgram doctor: connection OK` in Telegram, `RESULT: PASS`, exit code 0
- [ ] `mcgram doctor` with bad token (edit `.env`) → fails with clear error, exit 1
- [ ] Companion skill installed at `~/.claude/skills/mcgram/SKILL.md`

## Connect to Claude Code

```bash
claude mcp add --scope user mcgram --env MCGRAM_CONFIG=~/.mcgram/config.yaml -- mcgram
# Restart Claude Code, then /mcp
```

- [ ] `/mcp` lists `mcgram` as connected
- [ ] `/tools` (or equivalent) shows 6 tools: send_message, send_file, ask, set_reminder, cancel_reminder, list_reminders

## Tool round-trips (drive Claude Code directly)

- [ ] "Send me a Telegram message saying hi" → message arrives in your chat
- [ ] "Send me this README" → file arrives as attachment
- [ ] "Ask me on Telegram if I want to deploy, options Yes/No" → buttons appear, tap → Claude receives the value
- [ ] "Ask me an open question" → reply with text → Claude receives the freetext
- [ ] "Ask me with a 5s timeout, don't reply" → after 5s, message edited to `(timed out)`, Claude proceeds
- [ ] "Remind me in 10 seconds to check logs" → 10s later, `⏰ check logs` arrives
- [ ] "List my reminders" → shows the one above
- [ ] "Cancel reminder r_xxxx" → confirms cancellation

## Edge cases

- [ ] Send a file `>50 MB` → rejected with `file_too_large`
- [ ] Send a file outside CWD → rejected with `path_outside_cwd`
- [ ] Schedule 11 reminders → 11th rejected with `reminder_max_pending`
- [ ] Spam send_message 25 times → 21st rejected with `rate_limit_exceeded`
- [ ] Send a message from a different Telegram account to the bot → message ignored, audit shows `rejected reason:non_operator`
- [ ] Kill `mcgram` mid-poll (Ctrl+C) → clean shutdown, no asyncio warnings, lock file removed
- [ ] Start a second `mcgram` while one is running → fails with `LockHeldError`

## Audit verification

```bash
mcgram audit                # summary
mcgram audit --rejected     # see rejections above
mcgram audit --tool ask     # only ask calls
mcgram audit --since 5m     # last 5 minutes
mcgram audit --tail         # Ctrl+C to stop
```

- [ ] Summary shows correct counts
- [ ] Rejected reasons listed
- [ ] `--tail` prints new lines in real time as you make tool calls

## Tear down

```bash
# Unregister
claude mcp remove mcgram
# Clean
rm -rf ~/.mcgram
rm -rf ~/.claude/skills/mcgram
uv tool uninstall mcgram
```
