"""In-process reminder scheduler — fires `send_message(⏰ text)` after delay_s."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .errors import LimitError

if TYPE_CHECKING:
    from .audit import AuditLog
    from .config import Settings
    from .tg_client import TelegramClient

log = logging.getLogger("mcgram.reminders")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds")


class ReminderScheduler:
    """Stores pending reminders as asyncio.Tasks. State lost on process exit."""

    def __init__(self, client: TelegramClient, settings: Settings, audit: AuditLog) -> None:
        self._client = client
        self._settings = settings
        self._audit = audit
        self._tasks: dict[str, asyncio.Task] = {}
        self._meta: dict[str, dict[str, Any]] = {}

    def create(self, text: str, delay_s: int, channel: str | None = None) -> dict[str, Any]:
        limits = self._settings.limits
        if not text or not isinstance(text, str):
            raise LimitError("reminder_text_required")
        if len(text) > limits.reminder_text_max_chars:
            raise LimitError("reminder_text_too_long")
        if delay_s <= 0:
            raise LimitError("reminder_delay_must_be_positive")
        if delay_s > limits.reminder_max_delay_s:
            raise LimitError("reminder_max_delay_s")
        if len(self._tasks) >= limits.reminder_max_pending:
            raise LimitError("reminder_max_pending")
        # Validate channel up-front so the error surfaces synchronously.
        chat_id = self._settings.resolve_channel(channel)
        rid = "r_" + secrets.token_hex(4)
        fires_at = time.time() + delay_s
        self._meta[rid] = {"text": text, "fires_at": fires_at,
                           "channel": channel or "default"}
        self._tasks[rid] = asyncio.create_task(
            self._fire(rid, delay_s, text, chat_id),
            name=f"mcgram-reminder-{rid}",
        )
        return {"reminder_id": rid, "fires_at": _iso(fires_at),
                "channel": channel or "default"}

    async def _fire(self, rid: str, delay_s: float, text: str, chat_id: int) -> None:
        try:
            await asyncio.sleep(delay_s)
            await self._client.send_message(chat_id, f"⏰ {text}")
            self._audit.write({"tool": "_reminder_fire", "status": "ok", "rid": rid})
        except asyncio.CancelledError:
            self._audit.write({"tool": "_reminder_fire", "status": "cancelled", "rid": rid})
            raise
        except Exception as e:
            log.warning("reminder %s fire failed: %s", rid, e)
            self._audit.write({
                "tool": "_reminder_fire", "status": "error", "rid": rid, "error": str(e),
            })
        finally:
            self._tasks.pop(rid, None)
            self._meta.pop(rid, None)

    def cancel(self, rid: str) -> bool:
        task = self._tasks.pop(rid, None)
        if task is None:
            return False
        task.cancel()
        self._meta.pop(rid, None)
        return True

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "id": rid, "text": m["text"], "fires_at": _iso(m["fires_at"]),
                "channel": m.get("channel", "default"),
            }
            for rid, m in self._meta.items()
        ]

    def shutdown(self) -> None:
        """Cancel all pending reminder tasks. Call before event loop closes."""
        for rid, task in list(self._tasks.items()):
            task.cancel()
            self._tasks.pop(rid, None)
            self._meta.pop(rid, None)
