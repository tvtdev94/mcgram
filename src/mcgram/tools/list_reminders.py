"""Tool: list_reminders — list pending reminders."""

from __future__ import annotations

from typing import Any

from ..runtime import AppState

TOOL_NAME = "list_reminders"


def schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": "List pending reminders. Returns [{id, text, fires_at}].",
        "inputSchema": {"type": "object", "properties": {}},
    }


async def handle(state: AppState) -> dict[str, Any]:
    if state.reminders is None:
        return {"error": "internal", "reason": "reminders_not_initialized"}
    items = state.reminders.list()
    return {"reminders": items}
