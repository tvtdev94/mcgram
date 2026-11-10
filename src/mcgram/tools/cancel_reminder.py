"""Tool: cancel_reminder — cancel a pending reminder by id."""

from __future__ import annotations

from typing import Any

from ..runtime import AppState
from ..spool import valid_key

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
    if state.reminders.cancel(reminder_id):
        state.audit.write({"tool": TOOL_NAME, "status": "ok", "rid": reminder_id})
        return {"ok": True}
    owner_pid = _request_remote_cancel(state, reminder_id)
    if owner_pid is not None:
        state.audit.write({"tool": TOOL_NAME, "status": "requested", "via": "spool",
                           "rid": reminder_id, "owner_pid": owner_pid})
        # Honest wording: this is a request, and the owner may fire it first.
        return {"ok": True, "via": "spool",
                "note": f"cancel request sent to the session that owns this reminder "
                        f"(pid={owner_pid}); it still fires if it is already due"}
    state.audit.write({"tool": TOOL_NAME, "status": "not_found", "rid": reminder_id})
    return {"ok": False}


def _request_remote_cancel(state: AppState, reminder_id: str) -> int | None:
    """Owner pid when a cancel request was delivered to another session.

    Works whether or not the broker is on: saved reminders do not need it, and
    a reminder this session cannot cancel would otherwise still fire.
    """
    store = state.reminders.store if state.reminders is not None else None
    if store is None:
        return None
    if not isinstance(reminder_id, str) or not valid_key(reminder_id):
        return None
    return store.request_cancel(reminder_id)
