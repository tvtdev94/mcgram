"""Tool: send_message — post text to the operator chat."""

from __future__ import annotations

import time
from typing import Any

from ..errors import ConfigError, RateLimitError, TelegramError
from ..runtime import AppState

TOOL_NAME = "send_message"
_MAX_TEXT = 4096  # Telegram hard cap


def schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Send a text message to a configured Telegram channel. "
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
                    "description": "Override config default. 'plain' means no parse_mode.",
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
        chat_id = state.settings.resolve_channel(channel)
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
        msg = await state.client.send_message(
            chat_id,
            text,
            parse_mode=api_parse_mode,
            disable_notification=silent,
        )
    except TelegramError as e:
        state.audit.write({
            "tool": TOOL_NAME, "status": "error", "chat_id": chat_id,
            "text_len": len(text), "error": e.description,
        })
        return {"error": "telegram_api", "reason": e.description}
    ms = int((time.monotonic() - t0) * 1000)
    state.audit.write({
        "tool": TOOL_NAME, "status": "ok", "chat_id": chat_id,
        "channel": channel or "default",
        "text": text, "text_len": len(text), "ms": ms,
        "message_id": msg.get("message_id"),
    })
    return {"ok": True, "message_id": msg.get("message_id"),
            "channel": channel or "default"}
