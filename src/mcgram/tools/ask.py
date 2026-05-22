"""Tool: ask — post a question, await answer (button/freetext) or timeout."""

from __future__ import annotations

import time
from typing import Any

from ..errors import ConfigError, RateLimitError, TelegramError
from ..runtime import AppState

TOOL_NAME = "ask"


def schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Post a question to a configured Telegram channel and wait for a reply. "
            "Returns {value, source: button|freetext|timeout, question_id}. "
            "If options provided, posts inline buttons (1 per row). "
            "Blocks the MCP call until reply or timeout — keep timeout_s short."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Button labels (omit for freetext-only)",
                },
                "timeout_s": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Override default ask_timeout_s",
                },
                "channel": {
                    "type": "string",
                    "description": "Named channel from config (default: 'default')",
                },
            },
            "required": ["question"],
        },
    }


async def handle(
    state: AppState,
    *,
    question: str,
    options: list[str] | None = None,
    timeout_s: int | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    if not question or not isinstance(question, str):
        return {"error": "invalid_input", "reason": "question_required"}
    limits = state.settings.limits
    if options is not None:
        if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
            return {"error": "invalid_input", "reason": "options_must_be_list_of_str"}
        if len(options) == 0:
            options = None
        elif len(options) > limits.ask_options_max:
            return {
                "error": "invalid_input", "reason": "too_many_options",
                "max": limits.ask_options_max,
            }
    effective_timeout = timeout_s or state.settings.defaults.ask_timeout_s
    if effective_timeout > limits.ask_timeout_max_s:
        effective_timeout = limits.ask_timeout_max_s

    if state.ask_registry is None:
        return {"error": "internal", "reason": "ask_registry_not_initialized"}
    try:
        chat_id = state.settings.resolve_channel(channel)
    except ConfigError as e:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "unknown_channel", "channel": channel})
        return {"error": "invalid_input", "reason": "unknown_channel", "detail": str(e)}
    if not state.rate.try_acquire(TOOL_NAME):
        state.audit.write({"tool": TOOL_NAME, "status": "rejected", "reason": "rate_limit"})
        raise RateLimitError(TOOL_NAME)

    t0 = time.monotonic()
    try:
        result = await state.ask_registry.open(
            state.client,
            chat_id=chat_id,
            question=question,
            options=options,
            timeout_s=effective_timeout,
        )
    except TelegramError as e:
        state.audit.write({
            "tool": TOOL_NAME, "status": "error", "error": e.description,
        })
        return {"error": "telegram_api", "reason": e.description}
    ms = int((time.monotonic() - t0) * 1000)
    state.audit.write({
        "tool": TOOL_NAME, "status": "ok",
        "channel": channel or "default",
        "question_id": result["question_id"],
        "source": result["source"],
        "ms": ms,
    })
    result["channel"] = channel or "default"
    return result
