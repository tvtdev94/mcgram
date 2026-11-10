"""Reminder scheduler — fires `send_message(⏰ text)` after delay_s."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .dispatch import require_client, send_text
from .errors import ConfigError, LimitError

if TYPE_CHECKING:
    from .config import Destination
    from .reminder_spool import ReminderSpool
    from .runtime import AppState

log = logging.getLogger("mcgram.reminders")

#: How often orphaned reminders (owner process gone) are looked for. The first
#: look happens as soon as `start()` runs, so a restart delivers promptly.
ADOPT_INTERVAL_S = 5.0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds")


class ReminderScheduler:
    """Pending reminders as asyncio.Tasks, mirrored to disk when `store` is set.

    Delivery goes through `dispatch.send_text`, so a reminder reaches whichever
    transport its channel resolves to — telegram, ntfy, or discord.

    `reminder_max_pending` counts this process only: a cross-session cap would
    refuse a reminder here because some other session is busy.
    """

    def __init__(self, state: AppState, store: ReminderSpool | None = None) -> None:
        self._state = state
        self._settings = state.settings
        self._audit = state.audit
        self._tasks: dict[str, asyncio.Task] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self.store = store
        self._watch_task: asyncio.Task | None = None

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
        dest = self._settings.resolve_destination(channel)
        require_client(self._state, dest)
        rid = "r_" + secrets.token_hex(4)
        fires_at = time.time() + delay_s
        self._schedule(rid, text, fires_at, dest)
        if self.store is not None and not self.store.publish(
            rid, text=text, fires_at=fires_at, channel=dest.name, transport=dest.transport
        ):
            log.warning("reminder %s could not be saved; it will not survive a restart", rid)
        return {"reminder_id": rid, "fires_at": _iso(fires_at),
                "channel": dest.name, "transport": dest.transport}

    def _schedule(
        self, rid: str, text: str, fires_at: float, dest: Destination, *, late: bool = False
    ) -> None:
        body = f"⏰ {text}"
        if late:
            body += (f"\n\n(late: was due {_iso(fires_at)}; the mcgram session that "
                     "scheduled it stopped before it fired)")
        self._meta[rid] = {"text": text, "fires_at": fires_at,
                           "channel": dest.name, "transport": dest.transport}
        self._tasks[rid] = asyncio.create_task(
            self._fire(rid, max(0.0, fires_at - time.time()), body, dest),
            name=f"mcgram-reminder-{rid}",
        )

    async def _fire(self, rid: str, delay_s: float, body: str, dest: Destination) -> None:
        try:
            await asyncio.sleep(delay_s)
            await send_text(self._state, dest, body)
            self._audit.write({"tool": "_reminder_fire", "status": "ok",
                               "rid": rid, "transport": dest.transport})
        except asyncio.CancelledError:
            # Cancelled by `cancel()` (which retracts) or by shutdown — whose
            # whole point is to leave the record for the next process.
            self._audit.write({"tool": "_reminder_fire", "status": "cancelled", "rid": rid})
            raise
        except Exception as e:
            log.warning("reminder %s fire failed: %s", rid, e)
            self._audit.write({
                "tool": "_reminder_fire", "status": "error",
                "rid": rid, "transport": dest.transport, "error": str(e),
            })
        finally:
            self._tasks.pop(rid, None)
            self._meta.pop(rid, None)
        if self.store is not None:
            self.store.retract(rid)

    def cancel(self, rid: str) -> bool:
        task = self._tasks.pop(rid, None)
        if task is None:
            return False
        task.cancel()
        self._meta.pop(rid, None)
        if self.store is not None:
            self.store.retract(rid)
        return True

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "id": rid, "text": m["text"], "fires_at": _iso(m["fires_at"]),
                "channel": m.get("channel", "default"),
                "transport": m.get("transport", "telegram"),
            }
            for rid, m in self._meta.items()
        ]

    def start(self) -> None:
        """Start the disk watcher. Needs a running loop, so it is NOT done in
        `__init__`: the constructor stays sync and loop-free for its callers."""
        if self.store is not None and self._watch_task is None:
            self._watch_task = asyncio.create_task(self._watch(), name="mcgram-reminder-watch")

    def shutdown(self) -> None:
        """Cancel every task. Saved records stay on disk for the next process."""
        if self._watch_task is not None:
            self._watch_task.cancel()
            self._watch_task = None
        for rid, task in list(self._tasks.items()):
            task.cancel()
            self._tasks.pop(rid, None)
            self._meta.pop(rid, None)

    async def _watch(self) -> None:
        """Cancel mailbox every tick, adoption and sweep on slower cadences.

        Every step is synchronous, so cancellation can only land on the sleep —
        never between reading a cancel request and acting on it.
        """
        assert self.store is not None
        cfg = self._settings.broker
        tick = cfg.poll_interval_s if cfg.enabled else ADOPT_INTERVAL_S
        next_adopt = next_sweep = 0.0
        while True:
            try:
                for rid in self.store.take_cancel_requests(list(self._tasks)):
                    if self.cancel(rid):
                        self._audit.write({"tool": "cancel_reminder", "status": "ok",
                                           "rid": rid, "via": "broker"})
                now = time.monotonic()
                if now >= next_adopt:
                    next_adopt = now + ADOPT_INTERVAL_S
                    self._adopt_orphans()
                if now >= next_sweep:
                    next_sweep = now + cfg.sweep_interval_s
                    self.store.sweep()
            except Exception:
                log.exception("reminder watch tick failed")
            await asyncio.sleep(tick)

    def _adopt_orphans(self) -> None:
        """Take over reminders whose owner is gone; overdue ones fire at once."""
        assert self.store is not None
        limits, now = self._settings.limits, time.time()
        for rec in self.store.orphans():
            if len(self._tasks) >= limits.reminder_max_pending:
                return
            if (rec["rid"] in self._tasks
                    or len(rec["text"]) > limits.reminder_text_max_chars
                    or rec["fires_at"] - now > limits.reminder_max_delay_s):
                continue
            dest = self._deliverable(rec["channel"])
            if dest is None or self.store.claim(rec) is None:
                continue
            late = rec["fires_at"] <= now
            self._schedule(rec["rid"], rec["text"], rec["fires_at"], dest, late=late)
            self._audit.write({"tool": "_reminder_adopt", "status": "ok", "rid": rec["rid"],
                               "from_pid": rec["pid"], "late": late})

    def _deliverable(self, channel: str) -> Destination | None:
        """The destination, if THIS instance can deliver to it. Instances may
        run different configs, so an orphan we cannot deliver is left for one
        that can rather than dropped."""
        try:
            dest = self._settings.resolve_destination(channel)
            require_client(self._state, dest)
        except ConfigError:
            return None
        return dest
