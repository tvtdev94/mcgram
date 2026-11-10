"""Tool: edit_message — replace the text of a message sent earlier."""

from __future__ import annotations

import time
from typing import Any

from ..discord_client import webhook_id_from_url
from ..dispatch import edit_text
from ..errors import (
    ConfigError,
    DiscordError,
    NtfyError,
    RateLimitError,
    TelegramError,
    TransportError,
)
from ..runtime import AppState
from .send_message import _DEFAULT_MAX_TEXT, _MAX_TEXT_BY_TRANSPORT

TOOL_NAME = "edit_message"


def schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Replace the text of a message sent earlier with send_message, so one "
            "message can show live progress instead of many pings. Telegram and "
            "Discord only (ntfy → transport_unsupported). Edits never notify or "
            "ping anyone; @mentions are not supported."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": ["string", "integer"],
                    "description": "message_id returned by send_message",
                },
                "text": {"type": "string", "description": "New message body"},
                "channel": {
                    "type": "string",
                    "description": (
                        "Channel the message was sent to (default: 'default')"
                    ),
                },
                "thread_id": {
                    "type": "string",
                    "description": (
                        "Discord only: thread the message lives in, if it was "
                        "sent with thread_id."
                    ),
                },
                "parse_mode": {
                    "type": "string",
                    "enum": ["plain", "markdown_v2"],
                    "description": "Telegram-only override.",
                },
            },
            "required": ["message_id", "text"],
        },
    }


def _normalize_id(raw: Any) -> str | None:
    """Both transports use numeric IDs; anything else is rejected up front
    (a Discord ID also lands in the URL path, so it must be digits only)."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return None
    value = str(raw).strip()
    return value if value.isdigit() else None


async def handle(state: AppState, *, message_id: Any, text: str,
                 channel: str | None = None,
                 thread_id: str | None = None,
                 parse_mode: str | None = None) -> dict[str, Any]:
    if not text or not isinstance(text, str):
        return {"error": "invalid_input", "reason": "text_required"}
    msg_id = _normalize_id(message_id)
    if msg_id is None:
        return {"error": "invalid_input", "reason": "invalid_message_id"}
    try:
        dest = state.settings.resolve_destination(channel)
    except ConfigError as e:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "unknown_channel", "channel": channel})
        return {"error": "invalid_input", "reason": "unknown_channel", "detail": str(e)}
    if dest.transport == "ntfy":
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "transport_unsupported", "channel": dest.name})
        return {"error": "transport_unsupported", "transport": "ntfy",
                "reason": "ntfy cannot edit a sent message; send a new one instead"}
    max_text = _MAX_TEXT_BY_TRANSPORT.get(dest.transport, _DEFAULT_MAX_TEXT)
    if len(text) > max_text:
        return {"error": "invalid_input", "reason": "text_too_long",
                "max": max_text, "transport": dest.transport}
    if not state.rate.try_acquire(TOOL_NAME):
        state.audit.write({"tool": TOOL_NAME, "status": "rejected", "reason": "rate_limit"})
        raise RateLimitError(TOOL_NAME)

    mode = parse_mode or state.settings.defaults.parse_mode
    api_parse_mode = "MarkdownV2" if mode == "markdown_v2" else None
    base = {"tool": TOOL_NAME, "transport": dest.transport, "channel": dest.name,
            "message_id": msg_id, "text_len": len(text)}
    t0 = time.monotonic()
    try:
        edited = await edit_text(
            state, dest, msg_id, text,
            parse_mode=api_parse_mode if dest.transport == "telegram" else None,
            thread_id=thread_id if dest.transport == "discord" else None,
        )
    except TransportError as e:
        state.audit.write({**base, "status": "error",
                           "reason": "transport_unreachable", "error": e.detail})
        return {"error": "transport_unreachable", "transport": dest.transport,
                "reason": e.detail,
                "hint": "message was NOT edited — check network connectivity"}
    except (TelegramError, NtfyError, DiscordError) as e:
        reason = getattr(e, "description", str(e))
        state.audit.write({**base, "status": "error", "error": reason})
        return {"error": f"{dest.transport}_api", "reason": reason}
    except ConfigError as e:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "transport_unavailable", "channel": dest.name})
        return {"error": "transport_unavailable", "reason": str(e)}
    record: dict[str, Any] = {
        **base, "status": "ok", "chat_id": dest.chat_id, "text": text,
        "ms": int((time.monotonic() - t0) * 1000),
    }
    if dest.transport == "discord":
        # Audit the webhook ID only — never the full URL (it carries the token).
        record["discord_webhook_id"] = webhook_id_from_url(dest.discord_webhook_url or "")
        record["thread_id"] = thread_id
    state.audit.write(record)
    result: dict[str, Any] = {"ok": True, "message_id": edited["message_id"],
                              "channel": dest.name, "transport": dest.transport}
    if thread_id is not None and dest.transport != "discord":
        result["note"] = f"thread_id ignored for {dest.transport}"
    return result
