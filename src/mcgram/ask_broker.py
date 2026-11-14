"""Non-owner side of the ask broker: write a request, wait for the owner's answer.

Only the process holding the Telegram poll lock receives the operator's tap, so
a session without it cannot resolve an ask on its own. Instead of posting a
question nobody can answer, it drops a request file in the spool and waits for
the owner to serve it. See `ask_server` for the other half.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from .lock import _pid_alive, _read_pid
from .spool import Spool

log = logging.getLogger("mcgram.ask_broker")

# Bumped when the request/answer shape changes. An owner running a different
# protocol is treated as absent: better an immediate `polling_not_owned` than a
# request that sits unread until the caller times out.
PROTOCOL_VERSION = 1

HEARTBEAT_KEY = "u0"
HEARTBEAT_KIND = "heartbeat"


class AskBroker:
    """Client half of the broker. Owns no Telegram state — it only writes and waits."""

    def __init__(
        self,
        spool: Spool,
        lock_path: Path,
        *,
        poll_interval_s: float,
        ttl_pad_s: int,
        heartbeat_stale_s: int,
    ) -> None:
        self._spool = spool
        self._lock_path = lock_path
        self._poll_interval_s = poll_interval_s
        self._ttl_pad_s = ttl_pad_s
        self._heartbeat_stale_s = heartbeat_stale_s

    def owner_alive(self) -> tuple[bool, str]:
        """(alive, reason) — is there an owner that will actually serve a request?

        Checked before writing anything. A spool we can write to says nothing
        about whether someone is reading it: the owner may be in 409 backoff,
        may have died with the lock not yet reclaimed, or may be an older
        version that never reads requests. In all three the caller would block
        for the full timeout and then be told `source: "timeout"` — which reads
        as "the operator ignored you" when the question never reached anyone.
        """
        hb = self._spool.read(HEARTBEAT_KEY, HEARTBEAT_KIND)
        if hb is None:
            return False, "no mcgram instance is running the ask broker"
        if hb.get("protocol") != PROTOCOL_VERSION:
            return False, (
                f"the polling instance speaks broker protocol {hb.get('protocol')!r}, "
                f"this one needs {PROTOCOL_VERSION} — upgrade both sessions"
            )
        at = hb.get("at")
        if not isinstance(at, int | float) or isinstance(at, bool):
            return False, "broker heartbeat is malformed"
        age = time.time() - at
        if age > self._heartbeat_stale_s:
            return False, (
                f"broker heartbeat is {age:.0f}s stale "
                f"(owner pid={hb.get('pid')} may have died or lost connectivity)"
            )
        pid = hb.get("pid")
        if not isinstance(pid, int) or not _pid_alive(pid):
            return False, f"broker owner pid={pid} is no longer running"
        return True, ""

    def open_request(
        self,
        q_id: str,
        *,
        question: str,
        options: list[str],
        chat_id: int,
        timeout_s: int,
        multi_select: bool = False,
        columns: int = 1,
    ) -> bool:
        """Publish a question for the owner to post. False if the spool refused it."""
        now = time.time()
        return self._spool.write_exclusive(
            q_id,
            "request",
            {
                "q_id": q_id,
                "protocol": PROTOCOL_VERSION,
                "question": question,
                "options": options,
                "chat_id": chat_id,
                "timeout_s": timeout_s,
                "multi_select": multi_select,
                "columns": columns,
                "requester_pid": os.getpid(),
                "created_at": now,
                # Padded past the caller's deadline so the owner can still write
                # an answer that the final read below picks up.
                "expires_at": now + timeout_s + self._ttl_pad_s,
            },
        )

    async def wait_for_answer(self, q_id: str, *, timeout_s: float) -> dict[str, Any] | None:
        """Poll for the owner's answer. None means the deadline passed.

        The read after the loop is not redundant: an answer written during the
        last poll interval would otherwise be missed, and `close_request` would
        then unlink it — silently swallowing what the operator actually answered.
        """
        deadline = time.monotonic() + timeout_s
        holders: set[int] = set()
        while True:
            answer = self._read_trusted_answer(q_id, holders)
            if answer is not None:
                return answer
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(self._poll_interval_s, remaining))
        return self._read_trusted_answer(q_id, holders)

    def close_request(self, q_id: str) -> None:
        """Drop the request and its answer. Safe to call twice.

        The owner's `taken` claim is deliberately left for the sweep: it expires
        with the request. Deleting it here would let an owner serve the request
        a second time whenever the request itself survives — on Windows a
        concurrent reader makes its unlink fail — or whenever an owner listed it
        just before it was deleted.
        """
        self._spool.remove(q_id, "request", "answer")

    def _read_trusted_answer(
        self, q_id: str, holders: set[int] | None = None
    ) -> dict[str, Any] | None:
        """The answer, if trusted. `holders` collects every poll-lock holder
        seen during one wait; the current holder is added on each call."""
        holders = set() if holders is None else holders
        holder = _read_pid(self._lock_path)
        if holder is not None:
            holders.add(holder)
        answer = self._spool.read(q_id, "answer")
        if answer is None:
            return None
        return answer if self._answer_trusted(answer, holders, holder) else None

    def _answer_trusted(
        self, answer: dict[str, Any], holders: set[int], holder: int | None
    ) -> bool:
        """The answer must come from a process that held the poll lock while
        this side was waiting.

        This does not stop a determined attacker running as the same uid — they
        can read the lock file too. What it does is raise forgery from "write one
        file" to "win the poll lock", and leave the mismatch in the log.

        Any holder seen during the wait counts, not only the current one: an
        owner that shuts down writes its `cancelled` answer while it still holds
        the lock and releases the lock right after, usually before this side
        polls again.

        A rejected answer is treated as no answer. Nobody serves the request a
        second time — the owner's `taken` claim stays until it expires — so
        unless a trusted answer turns up, the caller waits out its timeout.
        """
        claimed = answer.get("answered_by_pid")
        if claimed is None:
            log.warning("broker answer has no answered_by_pid — rejecting")
            return False
        if claimed not in holders:
            log.warning(
                "broker answer claims pid=%s but the poll lock is held by pid=%s — "
                "rejecting (forged answer, or ownership just moved)",
                claimed,
                holder,
            )
            return False
        return True
