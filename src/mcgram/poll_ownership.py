"""Poll-loop ownership — the single-instance lock guards polling, not the process.

Telegram's `getUpdates` accepts exactly one client per bot token; a second
poller gets HTTP 409. So long-polling must be exclusive across processes.

Everything else mcgram does — `send_message` / `send_file` / `send_video` /
reminders / ntfy — is one-way HTTP and is perfectly safe from any number of
processes at once. Scoping the lock to the poll loop lets a second `mcgram`
(a second Claude Code session, which is the normal case) boot in **send-only
degraded mode** instead of exiting: every tool works except `ask`, which needs
inbound updates and therefore the poll loop.

Ownership can also transfer: while degraded, this process retries the lock, so
when the owner exits — or dies and leaves a stale lock — `ask` starts working
here without a restart.

`ask` liveness is a direct function of poll ownership, so this module also owns
the `AskRegistry` lifecycle: the registry is only visible on `AppState` while
this process holds the lock. Without that, `ask` would post a question to
Telegram and then block on a future that only the *owning* process can resolve.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import LockHeldError
from .lock import SingleInstanceLock

if TYPE_CHECKING:
    from .runtime import AppState

log = logging.getLogger("mcgram.poll_ownership")

#: How often a degraded instance re-checks whether the poll lock became free.
#: 30s trades a little latency for near-zero idle cost — taking over sooner
#: matters only right after the owning session exits.
RETRY_INTERVAL_S = 30.0
#: Test/ops override for the retry cadence, in seconds.
RETRY_INTERVAL_ENV = "MCGRAM_POLL_RETRY_S"


def retry_interval_s_default() -> float:
    raw = os.environ.get(RETRY_INTERVAL_ENV, "").strip()
    if not raw:
        return RETRY_INTERVAL_S
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s=%r is not a number — using %.0fs.", RETRY_INTERVAL_ENV,
                    raw, RETRY_INTERVAL_S)
        return RETRY_INTERVAL_S
    if value <= 0:
        log.warning("%s must be > 0 — using %.0fs.", RETRY_INTERVAL_ENV, RETRY_INTERVAL_S)
        return RETRY_INTERVAL_S
    return value


class PollOwnership:
    """Holds (or waits for) the poll lock and mirrors that state into `AppState`.

    `attach()` performs the first acquire synchronously so tool handlers never
    observe an undecided state; `supervise()` then runs the poll loop for as
    long as ownership lasts and retries while it doesn't.
    """

    def __init__(self, lock_path: str | Path, *, skip_lock: bool = False) -> None:
        # skip_lock (MCGRAM_SKIP_LOCK=1) bypasses the lock entirely — the
        # operator explicitly accepts the 409 risk of two pollers.
        self._lock: SingleInstanceLock | None = (
            None if skip_lock else SingleInstanceLock(lock_path)
        )
        self._registry: Any | None = None
        self._warned_pid: int | None = None
        self.owned = False
        self.owner_pid: int | None = None

    def acquire(self) -> bool:
        """Try to take the lock. False → another live mcgram owns polling."""
        if self._lock is None:
            self.owned, self.owner_pid = True, None
            return True
        try:
            self._lock.acquire()
        except LockHeldError as e:
            self.owned, self.owner_pid = False, e.pid
            return False
        self.owned, self.owner_pid = True, None
        return True

    def release(self) -> None:
        """Idempotent — safe to call from both the supervisor and shutdown."""
        if self._lock is not None:
            self._lock.release()
        self.owned = False

    def attach(self, state: AppState) -> bool:
        """Acquire once, synchronously, and reflect the outcome on `state`."""
        acquired = self.acquire()
        self.sync(state)
        return acquired

    def sync(self, state: AppState) -> None:
        """Mirror ownership onto `AppState` so `ask` can fail fast when degraded."""
        state.owns_polling = self.owned
        state.poll_owner_pid = self.owner_pid
        state.ask_registry = self._ensure_registry(state) if self.owned else None

    def _ensure_registry(self, state: AppState) -> Any | None:
        """Create the AskRegistry on first ownership; reuse it on re-promotion.

        The dispatcher handler is registered exactly once — recreating the
        registry on every promotion would stack duplicate handlers.
        """
        if state.client is None:
            return None
        if self._registry is None:
            from .ask_registry import AskRegistry

            registry = AskRegistry()
            registry.set_answer_hook(state.client.answer_callback_query)
            state.dispatcher.register(registry.handle_update)
            self._registry = registry
        return self._registry

    async def supervise(
        self,
        state: AppState,
        poll: Callable[[], Awaitable[None]],
        *,
        retry_interval_s: float | None = None,
    ) -> None:
        """Poll while this process owns the lock; wait and retry while it doesn't."""
        if retry_interval_s is None:
            retry_interval_s = retry_interval_s_default()
        while True:
            if not self.owned and not self.acquire():
                self.sync(state)
                self._log_degraded(retry_interval_s)
                await asyncio.sleep(retry_interval_s)
                continue
            self.sync(state)
            self._warned_pid = None
            log.info("Telegram poll ownership held by this process (pid=%d).", os.getpid())
            try:
                await poll()
            finally:
                self.release()
                self.sync(state)
            # poll_loop only returns on cancellation (which propagates instead
            # of reaching here); pause before re-arming so an unexpected return
            # can't spin.
            await asyncio.sleep(retry_interval_s)

    def _log_degraded(self, retry_interval_s: float) -> None:
        """Warn once per owner, then stay quiet — this repeats every retry."""
        if self._warned_pid == self.owner_pid:
            log.debug("still waiting for poll lock (owner pid=%s).", self.owner_pid)
            return
        self._warned_pid = self.owner_pid
        log.warning(
            "another mcgram instance (pid=%s) owns Telegram polling — running "
            "send-only here: send_message / send_file / send_video / reminders "
            "work, `ask` does not. Retrying every %.0fs.",
            self.owner_pid,
            retry_interval_s,
        )


async def cancel_task(task: asyncio.Task | None) -> None:
    """Cancel and await a task, swallowing the resulting CancelledError."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
