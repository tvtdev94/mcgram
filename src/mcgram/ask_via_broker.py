"""`ask` from a session that does not poll Telegram: hand it to the one that does.

Order matters. Nothing here touches Telegram: the request is written first and
the polling session posts it. Posting from this side and registering afterwards
would leave an unanswerable question in the chat whenever registration failed,
and the bot API offers no way to take it back.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from .spool import new_key

if TYPE_CHECKING:
    from .config import Destination
    from .runtime import AppState

TOOL_NAME = "ask"

# The owner times the question out at the same deadline we use (`created_at +
# timeout_s` in the request) and writes its verdict before touching the chat.
# This margin only covers that file write and our poll interval, so a
# last-second tap is the one reported rather than our own timeout.
ANSWER_GRACE_S = 2.0

_ANSWER_SOURCES = {"button", "freetext", "timeout"}


async def ask_via_broker(
    state: AppState,
    dest: Destination,
    *,
    question: str,
    options: list[str] | None,
    timeout_s: int,
    **extra: Any,
) -> dict[str, Any]:
    """Write a request, wait for the owner's answer, always clean up.

    `extra` carries optional request fields (button layout and the like) that
    the owner forwards to `AskRegistry.open` unchanged.
    """
    bot = state.settings.bot
    if bot is None or dest.chat_id != bot.operator_chat_id:
        # The poll loop drops every update not from operator_chat_id, so a reply
        # in any other chat can never resolve the question — in any session.
        return _reject(state, dest, "ask_requires_operator_chat",
                       "only operator_chat_id replies reach mcgram (the poll loop "
                       "filters every other chat)",
                       hint="use send_message for this channel")
    broker = state.ask_broker
    q_id = new_key("q")
    if not broker.open_request(q_id, question=question, options=options,
                               chat_id=dest.chat_id, timeout_s=timeout_s, **extra):
        return _reject(state, dest, "broker_unavailable",
                       "could not write the request to the broker spool; the "
                       "question was NOT posted",
                       hint="run `mcgram doctor`, or ask from the polling session")
    t0 = time.monotonic()
    try:
        answer = await broker.wait_for_answer(q_id, timeout_s=timeout_s + ANSWER_GRACE_S)
    finally:
        broker.close_request(q_id)
    ms = int((time.monotonic() - t0) * 1000)

    if answer is None:
        source, value, error = "timeout", _empty(extra), None
    else:
        source, value = answer.get("source"), answer.get("value")
        # Anything but a real outcome is an owner-side failure (rate limit,
        # Telegram error, shutdown). Reporting it as a timeout would tell
        # Claude the operator ignored a question they never saw.
        error = None if source in _ANSWER_SOURCES else str(source)
        if error is None and not _value_matches(value, extra):
            error = "internal_error"  # a malformed answer file is not an answer
    state.audit.write({
        "tool": TOOL_NAME, "status": "error" if error else "ok", "via": "broker",
        "channel": dest.name, "question_id": q_id, "source": source, "ms": ms,
        "answered_by_pid": answer.get("answered_by_pid") if answer else None,
    })
    if error is not None:
        return {"error": error, "reason": (answer or {}).get("detail", ""),
                "question_id": q_id, "channel": dest.name, "via": "broker"}
    return {"value": value, "source": source, "question_id": q_id,
            "channel": dest.name, "via": "broker"}


def _empty(extra: dict[str, Any]) -> Any:
    return [] if extra.get("multi_select") else ""


def _value_matches(value: Any, extra: dict[str, Any]) -> bool:
    """Multi-select answers are lists of labels; everything else is a string."""
    if extra.get("multi_select"):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return isinstance(value, str)


def _reject(
    state: AppState, dest: Destination, error: str, reason: str, *, hint: str
) -> dict[str, Any]:
    state.audit.write({"tool": TOOL_NAME, "status": "rejected", "via": "broker",
                       "reason": error, "channel": dest.name})
    return {"error": error, "reason": reason, "channel": dest.name, "hint": hint}
