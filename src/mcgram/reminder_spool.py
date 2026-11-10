"""Pending reminders on disk: they survive a restart and every session can see them.

One record per pending reminder (`r_x.rec.json`), owned by the process that
schedules it. Only the owner fires a reminder. When an owner dies — crash,
restart, closed session — its records become orphans, and any running instance
may adopt them. Adoption is a claim through `write_exclusive` on a kind tagged
with the dead owner's instance token, so for each generation of ownership
exactly one adopter wins, however many instances race for it.

Ownership is tracked by a per-process random token, with the pid only as a
liveness probe: a new process that happens to reuse a dead owner's pid must
still see that owner's records as orphans.

Delivery is at-least-once. A process killed between sending a reminder and
retracting its record fires it again after the next adoption.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
import uuid
from typing import Any

from .lock import _pid_alive
from .spool import Spool

log = logging.getLogger("mcgram.reminders")

#: Identifies this process as a record owner. Hex only, so it can tag a kind.
INSTANCE = uuid.uuid4().hex[:16]

#: How long past its due time an orphan is still worth delivering. Beyond this
#: the record expires and the sweep drops it unfired.
LATE_GRACE_S = 86400

_TOKEN_RE = re.compile(r"[0-9a-f]{1,32}")


class ReminderSpool:
    """Publish, list, cancel and adopt reminder records. Never raises on I/O."""

    def __init__(
        self,
        spool: Spool,
        *,
        cancel_ttl_s: int,
        tmp_grace_s: int = 300,
    ) -> None:
        self._spool = spool
        self._cancel_ttl_s = cancel_ttl_s
        self._tmp_grace_s = tmp_grace_s
        self._pid = os.getpid()
        self._instance = INSTANCE

    def publish(self, rid: str, *, text: str, fires_at: float, channel: str,
                transport: str) -> bool:
        return self._spool.write(rid, "rec", {
            "rid": rid, "text": text, "fires_at": fires_at,
            "channel": channel, "transport": transport,
            "pid": self._pid, "owner": self._instance,
            "expires_at": fires_at + LATE_GRACE_S,
        })

    def retract(self, rid: str) -> None:
        """The reminder fired or was cancelled: nobody may fire it again."""
        self._spool.remove(rid, "rec", "cancel")

    def owner_alive(self, rec: dict[str, Any]) -> bool:
        if rec.get("owner") == self._instance:
            return True
        pid = rec.get("pid")
        # Our own pid on a record we do not own means a previous process that
        # had this pid — it is gone.
        return isinstance(pid, int) and pid != self._pid and _pid_alive(pid)

    def list_all(self) -> list[dict[str, Any]]:
        """Every well-formed record, annotated with `owner_alive`."""
        return [{**rec, "owner_alive": self.owner_alive(rec)}
                for key, rec in self._spool.list("rec") if _well_formed(key, rec)]

    def orphans(self) -> list[dict[str, Any]]:
        """Records whose owner is gone. Read-only: `claim` takes one over."""
        return [rec for rec in self.list_all() if not rec.pop("owner_alive")]

    def claim(self, rec: dict[str, Any]) -> dict[str, Any] | None:
        """Take over an orphan. None if another instance won it, or if a cancel
        request was waiting for it (the record is then dropped, not fired)."""
        rid = rec["rid"]
        claim_kind = "claim" + rec["owner"]
        now = time.time()
        if not self._spool.write_exclusive(
            rid, claim_kind, {"pid": self._pid, "at": now, "expires_at": rec["expires_at"]}
        ):
            return None
        # The claim file stays until it expires: removing it now would let an
        # instance still holding the old record win the same claim again.
        if self._spool.read(rid, "cancel") is not None:
            log.info("reminder %s was cancelled while its owner was gone; dropping", rid)
            self.retract(rid)
            return None
        mine = {**rec, "pid": self._pid, "owner": self._instance}
        self._spool.write(rid, "rec", mine)
        return mine

    def request_cancel(self, rid: str) -> int | None:
        """Ask the owner to cancel. Returns the owner's pid, or None when no
        such reminder is on disk or the request could not be written."""
        rec = self._spool.read(rid, "rec")
        if rec is None:
            return None
        now = time.time()
        if not self._spool.write(rid, "cancel", {
            "requested_by": self._pid, "at": now, "expires_at": now + self._cancel_ttl_s,
        }):
            return None
        pid = rec.get("pid")
        return pid if isinstance(pid, int) else None

    def take_cancel_requests(self, rids: list[str]) -> list[str]:
        """Pending cancel requests for `rids`, consumed.

        Best-effort one-shot, not atomic: two readers can both see a request
        before either unlinks it. Harmless — cancelling twice is a no-op.
        Requests for anyone else's reminders expire and are swept.
        """
        pending = self._spool.keys("cancel")
        taken = [rid for rid in rids if rid in pending]
        for rid in taken:
            self._spool.remove(rid, "cancel")
        return taken

    def sweep(self) -> int:
        return self._spool.sweep(tmp_grace_s=self._tmp_grace_s)


def _finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _well_formed(key: str, rec: dict[str, Any]) -> bool:
    """Records are files any same-uid process can write; check before trusting."""
    return (
        rec.get("rid") == key
        and isinstance(rec.get("text"), str) and bool(rec["text"])
        and isinstance(rec.get("channel"), str)
        and isinstance(rec.get("transport"), str)
        and isinstance(rec.get("pid"), int) and not isinstance(rec["pid"], bool)
        and isinstance(rec.get("owner"), str) and bool(_TOKEN_RE.fullmatch(rec["owner"]))
        and _finite(rec.get("fires_at")) and _finite(rec.get("expires_at"))
    )
