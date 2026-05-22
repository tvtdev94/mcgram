"""Tool: send_message — post text to the operator chat (telegram or ntfy)."""

from __future__ import annotations

import time
from typing import Any

from ..dispatch import send_text
from ..errors import ConfigError, NtfyError, RateLimitError, TelegramError
from ..runtime import AppState

TOOL_NAME = "send_message"
_MAX_TEXT = 4096  # Telegram hard cap (ntfy allows more; keep unified)


def schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Send a text message to a configured channel (Telegram or ntfy.sh). "
            "Use for progress updates, completion pings, error reports."
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
            },
            "required": ["text"],
        },
    }


async def handle(state: AppState, *, text: str, channel: str | None = None,
                 silent: bool = False,
                 parse_mode: str | None = None) -> dict[str, Any]:
    if not text or not isinstance(text, str):
        return {"error": "invalid_input", "reason": "text_required"}
    if len(text) > _MAX_TEXT:
        return {"error": "invalid_input", "reason": "text_too_long", "max": _MAX_TEXT}
    try:
        dest = state.settings.resolve_destination(channel)
    except ConfigError as e:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "unknown_channel", "channel": channel})
        return {"error": "invalid_input", "reason": "unknown_channel", "detail": str(e)}
    if not state.rate.try_acquire(TOOL_NAME):
        state.audit.write({"tool": TOOL_NAME, "status": "rejected", "reason": "rate_limit"})
        raise RateLimitError(TOOL_NAME)

    mode = parse_mode or state.settings.defaults.parse_mode
    api_parse_mode = "MarkdownV2" if mode == "markdown_v2" else None
    t0 = time.monotonic()
    try:
        sent = await send_text(
            state, dest, text,
            silent=silent,
            parse_mode=api_parse_mode if dest.transport == "telegram" else None,
        )
    except (TelegramError, NtfyError) as e:
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
    state.audit.write({
        "tool": TOOL_NAME, "status": "ok",
        "transport": dest.transport, "channel": dest.name,
        "chat_id": dest.chat_id, "ntfy_topic": dest.ntfy_topic,
        "text": text, "text_len": len(text), "ms": ms,
        "message_id": sent["message_id"],
    })
    return {"ok": True, "message_id": sent["message_id"],
            "channel": dest.name, "transport": dest.transport}
