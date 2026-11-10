"""Owner side of the ask broker: serve spooled requests through the existing registry.

Runs only in the process holding the Telegram poll lock. Each request is served
by calling `AskRegistry.open()` unchanged — posting, awaiting, editing and
cleanup all stay where they already work, and a brokered ask becomes an ordinary
`_pending` entry. Nothing in `ask_registry` or the dispatcher knows this exists.

The loop lives in its own task, never inside the update dispatcher: scanning the
spool from a message handler would stall the 25s long-poll and drop updates.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from typing import TYPE_CHECKING, Any

from .ask_broker import HEARTBEAT_KEY, HEARTBEAT_KIND, PROTOCOL_VERSION
from .errors import TelegramError
from .spool import Spool

if TYPE_CHECKING:
    from .ask_registry import AskRegistry
    from .rate_limiter import RateLimiter
    from .tg_client import TelegramClient

log = logging.getLogger("mcgram.ask_server")

TOOL_NAME = "ask"


class AskServer:
    """Watches the spool for questions and answers them via the poll loop."""

    def __init__(
        self,
        spool: Spool,
        registry: AskRegistry,
        client: TelegramClient,
        rate: RateLimiter,
        audit: Any,
        *,
        poll_interval_s: float,
        answer_ttl_s: int,
        heartbeat_stale_s: int,
        tmp_grace_s: int = 300,
        sweep_interval_s: int = 60,
    ) -> None:
        self._spool = spool
        self._registry = registry
        self._client = client
        self._rate = rate
        self._audit = audit
        self._poll_interval_s = poll_interval_s
        self._answer_ttl_s = answer_ttl_s
        self._heartbeat_stale_s = heartbeat_stale_s
        self._tmp_grace_s = tmp_grace_s
        self._sweep_interval_s = sweep_interval_s
        self._serving: dict[str, asyncio.Task] = {}
        self._last_sweep = 0.0

    async def run(self) -> None:
        """Heartbeat, claim, serve — forever. Cancellable via `task.cancel()`.

        Each request is served in its own task so one slow question does not
        hold up the others, and any error in a tick is logged rather than
        allowed to kill the loop: a dead watcher looks exactly like "no owner"
        to every other session.

        Cancelling `run` also cancels and awaits every request in flight, so
        each one writes its `cancelled` answer before this returns — that is,
        before shutdown or demotion releases the poll lock. Nobody else serves
        a claimed request, so without that answer the requester would wait out
        its full timeout and then report that the operator ignored it.
        """
        try:
            while True:
                try:
                    self.write_heartbeat()
                    self._scan_once()
                    self._maybe_sweep()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("ask_server tick failed")
                await asyncio.sleep(self._poll_interval_s)
        finally:
            await self._stop_serving()

    async def _stop_serving(self) -> None:
        tasks = list(self._serving.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def write_heartbeat(self) -> bool:
        """Publish liveness so non-owners can fail fast instead of blocking."""
        now = time.time()
        return self._spool.write(
            HEARTBEAT_KEY,
            HEARTBEAT_KIND,
            {
                "pid": os.getpid(),
                "at": now,
                "protocol": PROTOCOL_VERSION,
                "expires_at": now + self._heartbeat_stale_s,
            },
        )

    def _scan_once(self) -> None:
        now = time.time()
        for key, req in self._spool.list("request"):
            if key in self._serving or _expired(req, now):
                continue
            if req.get("protocol") != PROTOCOL_VERSION:
                log.warning("ask_server: ignoring request %s with protocol %r", key,
                            req.get("protocol"))
                continue
            if not _well_formed(req):
                log.warning("ask_server: ignoring malformed request %s", key)
                continue
            # Claim before serving. O_EXCL makes this atomic even when two
            # owners exist at once, which MCGRAM_SKIP_LOCK=1 allows.
            # The claim expires with the request so a sweep can never drop it
            # while the operator is still deciding.
            if not self._spool.write_exclusive(
                key,
                "taken",
                {"owner_pid": os.getpid(), "at": now, "expires_at": req["expires_at"]},
            ):
                continue
            task = asyncio.create_task(self._serve_one(key, req), name=f"mcgram-ask-{key}")
            self._serving[key] = task
            task.add_done_callback(lambda t, k=key: self._serving.pop(k, None))

    def _maybe_sweep(self) -> None:
        now = time.time()
        if now - self._last_sweep < self._sweep_interval_s:
            return
        self._last_sweep = now
        removed = self._spool.sweep(now, tmp_grace_s=self._tmp_grace_s)
        if removed:
            log.debug("ask_server: swept %d stale spool records", removed)

    async def _serve_one(self, key: str, req: dict[str, Any]) -> None:
        """Post the question and record whatever comes back.

        An answer is written on every path, including failures. Without one the
        requester waits out its full timeout and then reports `source:
        "timeout"` — telling Claude the operator ignored the question when in
        fact Telegram errored.

        The question times out at the requester's deadline (`created_at +
        timeout_s`), not `timeout_s` after posting, and the outcome is written
        before the chat is edited. Either delay — a flood wait on the post or
        on the edit — would otherwise let the chat show an answer the requester
        never received because it had already given up.
        """
        answered = False

        def record(value: Any, source: str) -> None:
            nonlocal answered
            answered = True
            self._write_answer(key, value, source)

        try:
            # The budget belongs to the bot token, not to the calling session,
            # so it is spent here rather than by the requester.
            if not self._rate.try_acquire(TOOL_NAME):
                self._write_answer(key, "", "rate_limited")
                return
            await self._registry.open(
                self._client,
                chat_id=req["chat_id"],
                question=req["question"],
                options=req["options"],
                timeout_s=req["timeout_s"],
                multi_select=req.get("multi_select", False),
                columns=req.get("columns", 1),
                deadline=req["created_at"] + req["timeout_s"],
                on_outcome=record,
            )
        except asyncio.CancelledError:
            if not answered:
                self._write_answer(
                    key, "", "cancelled",
                    detail="the polling session stopped before the operator answered")
            raise
        except TelegramError as e:
            if not answered:
                self._write_answer(key, "", "telegram_error", detail=e.description)
        except Exception:
            log.exception("ask_server: serving %s failed", key)
            if not answered:
                self._write_answer(key, "", "internal_error")

    def _write_answer(
        self, key: str, value: str | list[str], source: str, *, detail: str | None = None
    ) -> None:
        now = time.time()
        payload: dict[str, Any] = {
            "value": value,
            "source": source,
            "answered_at": now,
            # Proves the answer came from the process holding the poll lock.
            # The requester cross-checks it against the lock file.
            "answered_by_pid": os.getpid(),
            "expires_at": now + self._answer_ttl_s,
        }
        if detail is not None:
            payload["detail"] = detail
        if not self._spool.write(key, "answer", payload):
            log.warning("ask_server: could not write answer for %s; requester will time out", key)
            return
        self._audit.write({
            "tool": TOOL_NAME,
            "status": "ok" if source in ("button", "freetext") else "error",
            "via": "broker",
            "question_id": key,
            "source": source,
            "answered_by_pid": os.getpid(),
        })


def _expired(req: dict[str, Any], now: float) -> bool:
    expires = req.get("expires_at")
    if not isinstance(expires, int | float) or isinstance(expires, bool):
        return True
    return expires < now


def _finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _well_formed(req: dict[str, Any]) -> bool:
    """Reject anything `AskRegistry.open` would choke on.

    Requests are files on disk, so a truncated write or a hand-edited record can
    reach here; failing the check leaves the request for the sweeper instead of
    raising inside the serve task.
    """
    if not isinstance(req.get("question"), str) or not req["question"]:
        return False
    if not _finite(req.get("created_at")):
        return False
    if not isinstance(req.get("chat_id"), int) or isinstance(req.get("chat_id"), bool):
        return False
    timeout = req.get("timeout_s")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        return False
    if not isinstance(req.get("multi_select", False), bool):
        return False
    columns = req.get("columns", 1)
    if not isinstance(columns, int) or isinstance(columns, bool) or not 1 <= columns <= 4:
        return False
    options = req.get("options")
    return options is None or (
        isinstance(options, list) and all(isinstance(o, str) for o in options)
    )
