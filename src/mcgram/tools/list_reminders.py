"""Tool: list_reminders — list pending reminders."""

from __future__ import annotations

import os
from typing import Any

from ..reminders import _iso
from ..runtime import AppState

TOOL_NAME = "list_reminders"


def schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "List pending reminders. Returns [{id, text, fires_at, channel, transport}]. "
            "When reminders are saved to disk (the default), reminders set in other "
            "sessions are included too and every entry adds {local, owner_pid, "
            "owner_alive}; cancel_reminder reaches all of them."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    }


async def handle(state: AppState) -> dict[str, Any]:
    if state.reminders is None:
        return {"error": "internal", "reason": "reminders_not_initialized"}
    items = state.reminders.list()
    store = state.reminders.store
    if store is None:
        return {"reminders": items}
    for item in items:
        item.update(local=True, owner_pid=os.getpid(), owner_alive=True)
    local_ids = {item["id"] for item in items}
    # Local wins on an id clash: this process's own view is authoritative.
    items += [
        {"id": rec["rid"], "text": rec["text"], "fires_at": _iso(rec["fires_at"]),
         "channel": rec["channel"], "transport": rec["transport"],
         "local": False, "owner_pid": rec["pid"], "owner_alive": rec["owner_alive"]}
        for rec in store.list_all() if rec["rid"] not in local_ids
    ]
    return {"reminders": items}
