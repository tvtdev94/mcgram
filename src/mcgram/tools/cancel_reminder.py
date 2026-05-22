"""Tool: cancel_reminder — cancel a pending reminder by id."""

from __future__ import annotations

from typing import Any

from ..runtime import AppState

TOOL_NAME = "cancel_reminder"


def schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": "Cancel a previously-scheduled reminder. Returns {ok: bool}.",
        "inputSchema": {
            "type": "object",
            "properties": {"reminder_id": {"type": "string"}},
            "required": ["reminder_id"],
        },
    }


async def handle(state: AppState, *, reminder_id: str) -> dict[str, Any]:
    if state.reminders is None:
        return {"error": "internal", "reason": "reminders_not_initialized"}
    ok = state.reminders.cancel(reminder_id)
    state.audit.write({
        "tool": TOOL_NAME,
        "status": "ok" if ok else "not_found",
        "rid": reminder_id,
    })
    return {"ok": ok}
