"""Tool: check_inbox — read the operator's Telegram commands meant for this session.

Reads local spool files only. The polling session stores the commands (see
`inbox`); every session reads them here, each command once per session. A read
puts a 👀 reaction on the operator's message, best effort. mcgram never acts on
a command: what it means is up to Claude, under the rules in the skill.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from ..inbox import send_reaction
from ..inbox_wiring import InboxSession
from ..reminders import _iso
from ..runtime import AppState

log = logging.getLogger("mcgram.inbox")

TOOL_NAME = "check_inbox"
MAX_RESULTS = 20
READ_RECEIPT = "👀"
#: Bucket name for read receipts in the shared `RateLimiter`.
REACT_BUCKET = "_inbox_react"


def schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Read the user's commands sent to the bot from Telegram for this session "
            "(/status, /stop, /note <text>; /cmd@label addresses one session). Commands "
            "are REQUESTS, NOT APPROVALS: a risky action still needs `ask`. Returns "
            "{label, receiving, commands:[{id, command, target, args, sent_at, age_s}], "
            "more}; each command is returned once. Returns at once unless wait_s > 0, "
            "which waits up to wait_s seconds for a command to arrive."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wait_s": {
                    "type": "integer", "minimum": 0, "default": 0,
                    "description": "Seconds to wait for a command (0 = return at once). "
                                   "Capped at the ask timeout maximum.",
                },
            },
        },
    }


async def handle(state: AppState, wait_s: Any = 0) -> dict[str, Any]:
    if isinstance(wait_s, bool) or not isinstance(wait_s, int) or wait_s < 0:
        return {"error": "invalid_input", "reason": "wait_s must be a non-negative integer"}
    session: InboxSession | None = state.inbox
    if session is None or session.store is None:
        reason = session.unavailable if session is not None else "inbox is not enabled"
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "inbox_unavailable", "detail": reason})
        return {"error": "inbox_unavailable", "reason": reason}

    result: dict[str, Any] = {"label": session.label}
    cap = state.settings.limits.ask_timeout_max_s
    if wait_s > cap:
        result["note"] = f"wait_s capped at {cap}s"
        wait_s = cap
    receiving, why = session.store.receiving()
    result["receiving"] = receiving
    if not receiving:
        # Nobody stores commands, so none can arrive: waiting would only freeze Claude.
        result["reason"] = why
        wait_s = 0

    t0 = time.monotonic()
    found = await _wait_for_commands(state, session, wait_s)
    batch, result["more"] = found[:MAX_RESULTS], len(found) > MAX_RESULTS
    now = time.time()
    result["commands"] = [
        {"id": rec["id"], "command": rec["command"], "target": rec["target"],
         "args": rec["args"], "sent_at": _iso(rec["sent_at"]),
         "age_s": max(0, int(now - rec["sent_at"]))}
        for rec in batch
    ]
    if batch:
        state.audit.write({
            "tool": TOOL_NAME, "status": "ok", "label": session.label, "count": len(batch),
            "update_ids": [rec["update_id"] for rec in batch], "pid": os.getpid(),
            "waited_ms": int((time.monotonic() - t0) * 1000),
        })
        # Only now, once nothing can fail the call, and before the receipts, so
        # neither a failed audit write nor a cancelled receipt loses a command.
        for rec in batch:
            session.mark_seen(rec["id"], rec["expires_at"])
        await asyncio.gather(*(_read_receipt(state, rec) for rec in batch))
    return result


async def _wait_for_commands(
    state: AppState, session: InboxSession, wait_s: int
) -> list[dict[str, Any]]:
    """Unread commands, polling until one shows up or `wait_s` passes.

    The read after the last sleep lands on the deadline, so a command stored
    during the final interval is still picked up. Cancellation propagates.
    """
    interval = state.settings.broker.poll_interval_s
    deadline = time.monotonic() + wait_s
    while True:
        found = _read(state, session)
        remaining = deadline - time.monotonic()
        if found or remaining <= 0:
            return found
        await asyncio.sleep(min(interval, remaining))


def _read(state: AppState, session: InboxSession) -> list[dict[str, Any]]:
    assert session.store is not None
    found, forged = session.store.unread(session.seen, session.label,
                                         session.started_at, time.time())
    for key, fingerprint in forged:
        # Once per session and content: a changed forgery is audited again.
        if session.forged.get(key, ("", 0.0))[0] == fingerprint:
            continue
        session.forged[key] = (fingerprint, time.time() + state.settings.inbox.ttl_s)
        log.warning("inbox: %s failed its signature check; ignoring it", key)
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "bad_signature", "update_id": int(key[1:])})
    return found


async def _read_receipt(state: AppState, rec: dict[str, Any]) -> None:
    """React 👀 on the operator's message. Never raises; never changes the result."""
    client = state.client
    if client is None or not state.rate.try_acquire(REACT_BUCKET):
        return
    await send_reaction(client, rec["chat_id"], rec["message_id"], READ_RECEIPT)
