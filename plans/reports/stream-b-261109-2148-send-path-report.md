# Stream B — send path (mcgram 0.6.0)

Status: done. Branch `worktree-agent-afb876c7ed90645fe`, fast-forwarded to the plan base `f127960` before any work (it had been created at `bd0087b`).

## Commits

- `555e58a` feat(transport): 429 retry, sendPhoto/sendAnimation, Discord webhook message edit, ntfy title/priority/tags on uploads, header line-break flattening
- `984bd5c` feat(tools): `edit_message`, ntfy options, long text as `.txt`, inline images
- `c6fc6dd` feat(cli): `mcgram test-send`
- `3b4a4e6` docs: README tools table / Discord / ntfy / CLI, SKILL.md send + edit sections

## What was done

1. **`edit_message`** (`tools/edit_message.py`, `dispatch.edit_text`, `DiscordClient.edit_message`). Send results already carried `message_id` (Telegram `message_id`, Discord message `id` via existing `?wait=true`, ntfy `id`), so no change was needed there. The ID must be numeric (checked before any network call; a Discord ID ends up in the URL path). ntfy returns `transport_unsupported` without a request. Discord edits send `allowed_mentions.parse = []`, so an edit never pings. Registered in `server._phase2_tool_modules`. Discord error code 10008 now maps to a readable reason.
2. **ntfy `priority`/`title`/`tags`** (`tools/_ntfy_options.py`). Checked on every transport (priority must be an int from 1 to 5, bools rejected; tags must be a list of non-empty strings without commas). Telegram/Discord drop them and add `note: "priority/title/tags ignored for <t>"`.
3. **Long text**. Over the transport cap, `send_message` uploads `message.txt` (UTF-8) from a private `TemporaryDirectory` that is deleted afterwards (`tools/_long_text.py`). The caption is the start of the text, including any Discord mention prefix, cut to `limits.caption_max_chars` (≤1024). The result adds `delivered_as: "file"` and `bytes`. Text over the transport file cap returns `text_too_long` with `max_bytes`. The upload skips the user-path CWD check.
4. **Telegram 429**. `_call` retries once after `parameters.retry_after` when it is ≤30s; a longer wait fails at once. Uploads now pass paths and are re-opened on every attempt. `getUpdates` is excluded (`retry_flood_wait=False`) because `poll_loop` already backs off. Discord got the same single retry (body `retry_after`, ≤30s).
5. **Inline images**. On Telegram, png/jpg/jpeg/webp go through `sendPhoto` (≤10 MB) and gif through `sendAnimation`. When Telegram answers a photo with 400/413, the file is resent as a document with a `note`. Other errors (403, 429) are not resent, so nothing is posted twice. Images over 10 MB go straight to `sendDocument`. `as_document=true` forces a document. Discord already sends the image MIME type, so it renders inline (no change). ntfy already sends `Filename` with the extension plus an image `Content-Type` (no change). `sent_as` is `photo`, `animation` or `document`.
6. **`mcgram test-send <channel> [--text T] | --all`** (`cli_test_send.py`). It runs the real `send_message.handle` (caps, rate limit, dispatch, audit) on clients built by `server._build_clients`. It prints one line per channel and exits 1 on any failure (2 on usage errors).

Side fix: ntfy header values now have CR/LF flattened to spaces. A multi-line caption would otherwise be refused by h11 (`Illegal header value b'a\nb'`, checked locally). Long-text captions are usually multi-line, so this fix is required for item 3.

## API docs checked (2026-11-09)

- Telegram `core.telegram.org/bots/api/methods/sendphoto.md`: photo ≤10 MB, width+height ≤10000, ratio ≤20, caption 0-1024.
- Telegram `.../methods/sendanimation.md`: GIF or H.264 without sound, ≤50 MB.
- Telegram `.../methods/editmessagetext.md`: chat_id + message_id + text 1-4096, returns the Message.
- Telegram `.../types/responseparameters.md`: `retry_after` = seconds left before the request can be repeated.
- Telegram `core.telegram.org/bots/api` "Making requests": `error_code` and optional `parameters` on failures.
- Discord `docs.discord.com/developers/resources/webhook`: Execute Webhook `wait` returns the message, `thread_id` is a query param. Edit Webhook Message is `PATCH /webhooks/{id}/{token}/messages/{message.id}` with `thread_id` query, content ≤2000, returns the message.
- ntfy `docs.ntfy.sh/publish/`: `Priority` 1-5, `Title`, comma-separated `Tags`, `Filename`. Publish response has `id`. ntfy.sh attachments ≤15 MB. Messages over 4096 bytes are turned into attachments by the server. Release notes say the apps preview images (GIF/WebP added in Android 1.18.0).

## Decisions worth a look at integration

- `sent_as` can also be `animation` (gif on Telegram), which goes beyond the `photo|document` named in the brief. On Discord/ntfy, `sent_as` is `photo` for image files whatever `as_document` says, because those clients always preview them.
- New private helper modules `tools/_ntfy_options.py` and `tools/_long_text.py` are new files, so they cannot clash with Stream A. `send_file.file_cap()` is shared by the send tools.
- `mcgram test-send --all` sends to every channel name, including an auto-seeded `default` that may point at the same chat as another channel.
- Not touched (outside ownership): README architecture tree (`tools/` list, `cli_test_send`), README "Multiple Claude Code sessions" table, CHANGELOG.

## Tests

- Baseline 482 passed / 7 skipped. Now **536 passed / 7 skipped** (`uv run pytest -q`, 92s). `uv run ruff check src/ tests/`: all checks passed.
- New or changed: `test_tg_client.py` (+4 flood-wait), `test_discord_client.py` (+3 rate limit, +3 edit; the old "429 raises at once" test now covers "still 429 after one retry"), `test_ntfy_client.py` (+2), `test_tools_send_message.py` (+3; old `text_too_long` test now covers the file-cap bound), `test_tools_send_file.py` (+7 images/options), `test_tools_ntfy.py` (+3 options param across tools, +8 invalid inputs, +long text, +edit unsupported), `test_tools_discord.py` (2 old reject tests converted to the attachment contract, +4 edit/image), new `test_tools_edit_message.py` (11), new `test_cli_test_send.py` (6, 1.9s).
- Not covered: live sends (forbidden in this stream; the orchestrator's smoke test covers them), real h11 rejection (pytest-httpx skips h11, so the header test checks the flattened value instead).
