"""Telegram long-poll loop with exponential backoff."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .errors import TelegramError
from .update_dispatcher import from_operator

if TYPE_CHECKING:
    from .audit import AuditLog
    from .tg_client import TelegramClient
    from .update_dispatcher import UpdateDispatcher

log = logging.getLogger("mcgram.polling")

_BACKOFF_MAX_S = 30.0
_LONG_POLL_TIMEOUT_S = 25
_CONFLICT_BACKOFF_S = 10.0  # Telegram 409 means another instance is polling.


async def poll_loop(
    client: TelegramClient,
    dispatcher: UpdateDispatcher,
    operator_chat_id: int,
    audit: AuditLog,
    *,
    long_poll_timeout_s: int = _LONG_POLL_TIMEOUT_S,
    initial_backoff_s: float = 1.0,
) -> None:
    """Long-poll getUpdates forever. Cancellable via `task.cancel()`."""
    offset = 0
    backoff = initial_backoff_s
    while True:
        try:
            updates = await client.get_updates(offset=offset, timeout=long_poll_timeout_s)
            backoff = 1.0
            for upd in updates:
                offset = upd["update_id"] + 1
                if not from_operator(upd, operator_chat_id):
                    audit.write({
                        "tool": "_polling", "status": "rejected",
                        "reason": "non_operator", "update_id": upd["update_id"],
                    })
                    continue
                await dispatcher.dispatch(upd)
        except asyncio.CancelledError:
            raise
        except TelegramError as e:
            if e.status == 409:
                # Another mcgram instance (or another bot client) is polling.
                # Back off longer; the other instance owns the updates.
                log.warning(
                    "poll_loop: 409 Conflict — another instance is polling this bot. "
                    "Sleeping %.0fs.", _CONFLICT_BACKOFF_S,
                )
                audit.write({
                    "tool": "_polling", "status": "conflict",
                    "reason": "409_another_poller", "detail": e.description,
                })
                try:
                    await asyncio.sleep(_CONFLICT_BACKOFF_S)
                except asyncio.CancelledError:
                    raise
                continue
            log.warning("poll_loop telegram error: %s", e)
            audit.write({"tool": "_polling", "status": "error", "error": str(e)})
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, _BACKOFF_MAX_S)
        except Exception as e:
            log.warning("poll_loop error: %s", e)
            audit.write({"tool": "_polling", "status": "error", "error": str(e)})
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, _BACKOFF_MAX_S)
