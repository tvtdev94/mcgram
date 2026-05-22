"""Tool: set_reminder — schedule an in-process reminder."""

from __future__ import annotations

from typing import Any

from ..errors import ConfigError, LimitError, RateLimitError
from ..runtime import AppState

TOOL_NAME = "set_reminder"


def schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Schedule a reminder to fire after delay_s seconds to a Telegram channel. "
            "Reminder is lost on process exit (in-memory scheduler). "
            "Returns {reminder_id, fires_at, channel}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "delay_s": {"type": "integer", "minimum": 1},
                "channel": {
                    "type": "string",
                    "description": "Named channel from config (default: 'default')",
                },
            },
            "required": ["text", "delay_s"],
        },
    }


async def handle(state: AppState, *, text: str, delay_s: int,
                 channel: str | None = None) -> dict[str, Any]:
    if state.reminders is None:
        return {"error": "internal", "reason": "reminders_not_initialized"}
    if not state.rate.try_acquire(TOOL_NAME):
        state.audit.write({"tool": TOOL_NAME, "status": "rejected", "reason": "rate_limit"})
        raise RateLimitError(TOOL_NAME)
    try:
        result = state.reminders.create(text, delay_s, channel=channel)
    except LimitError as e:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected", "reason": e.kind})
        return {"error": "rejected", "reason": e.kind}
    except ConfigError as e:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "unknown_channel", "channel": channel})
        return {"error": "invalid_input", "reason": "unknown_channel", "detail": str(e)}
    state.audit.write({
        "tool": TOOL_NAME, "status": "ok",
        "rid": result["reminder_id"], "fires_at": result["fires_at"],
        "channel": result["channel"],
    })
    return result
