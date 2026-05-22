"""Fan-out incoming Telegram updates to registered handler coroutines."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("mcgram.dispatcher")

UpdateHandler = Callable[[dict[str, Any]], Awaitable[None]]


class UpdateDispatcher:
    """Holds a list of async handlers. Each handler decides whether the update is for it.

    Exceptions raised by a handler are logged but never abort the polling loop.
    """

    def __init__(self) -> None:
        self._handlers: list[UpdateHandler] = []

    def register(self, handler: UpdateHandler) -> None:
        self._handlers.append(handler)

    async def dispatch(self, update: dict[str, Any]) -> None:
        for h in self._handlers:
            try:
                await h(update)
            except Exception:
                log.exception("update handler %r raised", getattr(h, "__qualname__", h))


def from_operator(update: dict[str, Any], operator_chat_id: int) -> bool:
    """Return True iff the update originates from the configured operator chat."""
    # callback_query carries its own message → chat
    cq = update.get("callback_query")
    if cq:
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        return chat.get("id") == operator_chat_id
    # text message
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    return chat.get("id") == operator_chat_id
