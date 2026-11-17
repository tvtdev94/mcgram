"""Keeps one subscription open on the ntfy reply topic and hands each message on.

Lifecycle: `NtfyAsk` starts the listener when a question opens and stops it
when the last one closes, so an idle mcgram holds no connection.

The cursor is the id of the last message seen or, before any message, the
`time` of the first `open` event. Both come from the server, so the local clock
never enters a `since=` comparison. A reconnect resumes from the cursor, which
is how a tap made while the laptop slept still arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .errors import AuthError, NtfyError, TransportError

if TYPE_CHECKING:
    from .audit import AuditLog
    from .ntfy_client import NtfyClient

log = logging.getLogger("mcgram.ntfy_reply")

AUDIT_TOOL = "_ntfy_reply"
_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 30.0
#: The catch-up read runs after the ask's deadline, so it gets its own short
#: budget rather than the client's 30 s request timeout.
_CATCH_UP_TIMEOUT_S = 5.0
# Before an `open` with a usable `time` arrives there is no server-side cursor;
# replaying the whole cache is safe because ids and tokens are random.
_SINCE_ALL = "all"


class ReplyListener:
    def __init__(
        self,
        client: NtfyClient,
        topic: str,
        *,
        on_message: Callable[[dict[str, Any]], None],
        on_fatal: Callable[[str], None],
        audit: AuditLog,
    ) -> None:
        self._client = client
        self._topic = topic
        self._on_message = on_message
        self._on_fatal = on_fatal
        self._audit = audit
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._cursor: str | None = None

    @property
    def connected(self) -> bool:
        """True while the current connection has seen its `open` event."""
        return self._ready.is_set()

    def ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._ready = asyncio.Event()
            self._cursor = None
            self._task = asyncio.create_task(self._run(), name="mcgram-ntfy-reply-listener")

    async def wait_ready(self, timeout: float) -> bool:
        """Wait until the subscription is open. False on timeout or if the
        listener gave up (an auth failure ends it)."""
        task = self._task
        if task is None:
            return False
        ready = asyncio.ensure_future(self._ready.wait())
        try:
            await asyncio.wait({ready, task}, timeout=timeout,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            ready.cancel()
        return self._ready.is_set()

    async def catch_up(self) -> bool:
        """Read what the reply topic cached since the cursor. False if that failed."""
        try:
            async with asyncio.timeout(_CATCH_UP_TIMEOUT_S):
                events = await self._client.poll(
                    self._topic, since=self._cursor or _SINCE_ALL)
        except (TransportError, NtfyError, AuthError, TimeoutError) as e:
            log.debug("ntfy reply catch-up failed: %s", e)
            return False
        for event in events:
            self._dispatch(event)
        return True

    async def stop(self) -> None:
        # Detach first: a question that opens while this one is being awaited
        # must start a fresh listener, not wait on the dying one.
        task, self._task = self._task, None
        self._ready = asyncio.Event()
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        ready = self._ready
        try:
            await self._subscribe_forever(ready)
        finally:
            # However the task ends, it is no longer listening.
            ready.clear()

    async def _subscribe_forever(self, ready: asyncio.Event) -> None:
        backoff = _BACKOFF_INITIAL_S
        opened = False
        while True:
            # The first connect needs no `since`: nothing is posted until it opens.
            since = self._cursor or (_SINCE_ALL if opened else None)
            try:
                async with contextlib.aclosing(
                    self._client.stream(self._topic, since=since)
                ) as events:
                    async for event in events:
                        kind = event.get("event")
                        if kind == "open":
                            if self._cursor is None and isinstance(event.get("time"), int):
                                self._cursor = str(event["time"])
                            opened = True
                            ready.set()
                            backoff = _BACKOFF_INITIAL_S
                        elif kind == "message":
                            self._dispatch(event)
                error = "subscription closed by the server"
            except AuthError:
                # The token or the ACL is wrong; retrying cannot fix it.
                self._audit.write({"tool": AUDIT_TOOL, "status": "error", "reason": "auth"})
                self._on_fatal("ntfy_auth")
                return
            except (TransportError, NtfyError) as e:
                error = str(e)
            except Exception as e:
                log.exception("ntfy reply subscription failed unexpectedly")
                error = type(e).__name__
            ready.clear()
            log.warning("ntfy reply subscription down (%s); retrying in %.0fs", error, backoff)
            self._audit.write({"tool": AUDIT_TOOL, "status": "error", "error": error})
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)

    def _dispatch(self, event: dict[str, Any]) -> None:
        if event.get("event") != "message":
            return
        if isinstance(event.get("id"), str):
            self._cursor = event["id"]
        try:
            self._on_message(event)
        except Exception:
            # A bug in answer matching must not take the subscription down.
            log.exception("handling an ntfy reply failed")
