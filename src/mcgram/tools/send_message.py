"""Tool: send_message — post text to the operator chat (telegram or ntfy)."""

from __future__ import annotations

import time
from typing import Any

from ..discord_client import format_mention_prefix, webhook_id_from_url
from ..dispatch import send_text
from ..errors import ConfigError, DiscordError, NtfyError, RateLimitError, TelegramError
from ..runtime import AppState
from ._mentions import resolve_for_dest

TOOL_NAME = "send_message"
# Text hard caps differ per transport; resolved after we know the destination.
_MAX_TEXT_BY_TRANSPORT = {
    "telegram": 4096,  # Telegram hard cap
    "ntfy": 4096,      # ntfy allows more; keep the prior unified cap unchanged
    "discord": 2000,   # Discord hard limit
}
_DEFAULT_MAX_TEXT = 4096


def schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Send a text message to a configured channel (Telegram, ntfy.sh, or "
            "Discord). Use for progress updates, completion pings, error reports. "
            "Discord channels have no default — name the channel explicitly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message body"},
                "channel": {
                    "type": "string",
                    "description": "Named channel from config (default: 'default')",
                },
                "silent": {
                    "type": "boolean",
                    "default": False,
                    "description": "Send without notification sound",
                },
                "parse_mode": {
                    "type": "string",
                    "enum": ["plain", "markdown_v2"],
                    "description": "Telegram-only override. ntfy channels ignore.",
                },
                "thread_id": {
                    "type": "string",
                    "description": (
                        "Discord only: ID of an existing thread to post into. Omit "
                        "to post to the base channel. Telegram/ntfy ignore it."
                    ),
                },
                "mention": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Discord only: names registered via "
                        "`mcgram discord mention add`. Prepends @mentions that ping "
                        "those users. Unregistered name → unknown_mention error. "
                        "Ignored on Telegram/ntfy."
                    ),
                },
            },
            "required": ["text"],
        },
    }


async def handle(state: AppState, *, text: str, channel: str | None = None,
                 silent: bool = False,
                 parse_mode: str | None = None,
                 thread_id: str | None = None,
                 mention: list[str] | None = None) -> dict[str, Any]:
    if not text or not isinstance(text, str):
        return {"error": "invalid_input", "reason": "text_required"}
    # Resolve first — the text length cap depends on the destination transport.
    try:
        dest = state.settings.resolve_destination(channel)
    except ConfigError as e:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "unknown_channel", "channel": channel})
        return {"error": "invalid_input", "reason": "unknown_channel", "detail": str(e)}
    mention_ids, mention_note, mention_err = resolve_for_dest(dest, mention)
    if mention_err is not None:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "unknown_mention", "channel": dest.name,
                           "unknown": mention_err["unknown"]})
        return mention_err
    # Prepend the @mention run so the cap check counts it (Discord rejects >2000).
    content = format_mention_prefix(mention_ids) + text if mention_ids else text
    max_text = _MAX_TEXT_BY_TRANSPORT.get(dest.transport, _DEFAULT_MAX_TEXT)
    if len(content) > max_text:
        return {"error": "invalid_input", "reason": "text_too_long",
                "max": max_text, "transport": dest.transport}
    if not state.rate.try_acquire(TOOL_NAME):
        state.audit.write({"tool": TOOL_NAME, "status": "rejected", "reason": "rate_limit"})
        raise RateLimitError(TOOL_NAME)

    mode = parse_mode or state.settings.defaults.parse_mode
    api_parse_mode = "MarkdownV2" if mode == "markdown_v2" else None
    t0 = time.monotonic()
    try:
        sent = await send_text(
            state, dest, content,
            silent=silent,
            parse_mode=api_parse_mode if dest.transport == "telegram" else None,
            thread_id=thread_id,
            mention_user_ids=mention_ids or None,
        )
    except (TelegramError, NtfyError, DiscordError) as e:
        state.audit.write({
            "tool": TOOL_NAME, "status": "error",
            "transport": dest.transport, "channel": dest.name,
            "text_len": len(text), "error": getattr(e, "description", str(e)),
        })
        return {"error": f"{dest.transport}_api",
                "reason": getattr(e, "description", str(e))}
    except ConfigError as e:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "transport_unavailable", "channel": dest.name})
        return {"error": "transport_unavailable", "reason": str(e)}
    ms = int((time.monotonic() - t0) * 1000)
    record: dict[str, Any] = {
        "tool": TOOL_NAME, "status": "ok",
        "transport": dest.transport, "channel": dest.name,
        "chat_id": dest.chat_id, "ntfy_topic": dest.ntfy_topic,
        "text": content, "text_len": len(content), "ms": ms,
        "message_id": sent["message_id"],
    }
    if dest.transport == "discord":
        # Audit the webhook ID only — never the full URL (it carries the token).
        record["discord_webhook_id"] = webhook_id_from_url(dest.discord_webhook_url or "")
        record["thread_id"] = thread_id
        if mention_ids:
            record["mentions"] = list(mention or [])
    state.audit.write(record)
    result: dict[str, Any] = {"ok": True, "message_id": sent["message_id"],
                              "channel": dest.name, "transport": dest.transport}
    notes = [n for n in (mention_note,) if n]
    if thread_id is not None and dest.transport != "discord":
        notes.append(f"thread_id ignored for {dest.transport}")
    if notes:
        result["note"] = "; ".join(notes)
    return result
