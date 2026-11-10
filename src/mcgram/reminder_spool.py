"""Pending reminders on disk: they survive a restart and every session can see them.

One record per pending reminder (`r_x.rec.json`), owned by the process that
schedules it. Only the owner fires a reminder. When an owner dies — crash,
restart, closed session — its records become orphans, and any running instance
may adopt them. Adoption is a claim through `write_exclusive` on a kind tagged
with the dead owner's instance token, so for each generation of ownership
exactly one adopter wins, however many instances race for it.

Ownership is tracked by a per-process random token. An owner counts as alive
only while its pid runs AND it keeps refreshing its heartbeat
(`o_<token>.alive`, every watcher tick). The pid alone is not enough: Windows
reuses pids, and an unrelated process holding a dead owner's pid would keep
that owner's reminders from ever firing.

A reminder that fired or was cancelled gets a `done` tombstone, created with
`write_exclusive` and kept as long as the record could live. Nobody fires or
adopts a reminder with a tombstone, so the record being left behind — on
Windows a concurrent reader makes its unlink fail — cannot fire it twice. A
cancel from another session is the same tombstone, which is why it reaches the
owner without the broker and still holds when an adopter turns up hours later.

Delivery is at-least-once only in one window: a process killed after sending a
reminder but before writing its tombstone fires it again after adoption.
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
_HEARTBEAT_KIND = "alive"
_DONE_KIND = "done"


class ReminderSpool:
    """Publish, list, cancel and adopt reminder records. Never raises on I/O."""

    def __init__(
        self,
        spool: Spool,
        *,
        cancel_ttl_s: int,
        heartbeat_stale_s: int = 90,
        tmp_grace_s: int = 300,
    ) -> None:
        self._spool = spool
        self._cancel_ttl_s = cancel_ttl_s
        self._heartbeat_stale_s = heartbeat_stale_s
        self._tmp_grace_s = tmp_grace_s
        self._pid = os.getpid()
        self._instance = INSTANCE
        # Owners found silent (pid alive, heartbeat stale) on the last look.
        self._silent: set[str] = set()

    def beat(self) -> bool:
        """Refresh this owner's heartbeat. Called on every watcher tick."""
        now = time.time()
        return self._spool.write(f"o_{self._instance}", _HEARTBEAT_KIND, {
            "pid": self._pid, "at": now, "expires_at": now + self._heartbeat_stale_s,
        })

    def publish(self, rid: str, *, text: str, fires_at: float, channel: str,
                transport: str) -> bool:
        # Beat first: a record whose owner has no heartbeat yet looks orphaned.
        self.beat()
        return self._spool.write(rid, "rec", {
            "rid": rid, "text": text, "fires_at": fires_at,
            "channel": channel, "transport": transport,
            "pid": self._pid, "owner": self._instance,
            "expires_at": fires_at + LATE_GRACE_S,
        })

    def finish(self, rid: str, fires_at: float) -> bool:
        """The reminder fired or was cancelled: nobody may fire it again.

        The tombstone goes first, so even a record that cannot be deleted now
        is never fired again. False when the record survived; the caller
        retries with `remove_record`.
        """
        now = time.time()
        self._spool.write_exclusive(rid, _DONE_KIND, {
            "at": now, "expires_at": max(fires_at + LATE_GRACE_S, now + self._cancel_ttl_s),
        })
        return self.remove_record(rid)

    def remove_record(self, rid: str) -> bool:
        return self._spool.remove(rid, "rec")

    def still_mine(self, rid: str) -> bool:
        """Checked right before firing: not cancelled, and not adopted away.

        A record we cannot read counts as ours — on Windows a concurrent reader
        can make the read fail, and skipping a due reminder is worse.
        """
        if self._is_finished(rid):
            return False
        rec = self._spool.read(rid, "rec")
        return rec is None or rec.get("owner") == self._instance

    def _is_finished(self, rid: str) -> bool:
        """The tombstone exists. Its content is not read: `write_exclusive`
        creates the file before filling it, so it can be empty for a moment."""
        path = self._spool.path_for(rid, _DONE_KIND)
        return path is not None and path.exists()

    def _owner_state(self, rec: dict[str, Any], now: float) -> str:
        """"alive", "dead" (pid gone), or "silent" (pid runs, heartbeat stale)."""
        if rec.get("owner") == self._instance:
            return "alive"
        pid = rec.get("pid")
        # Our own pid on a record we do not own means a previous process that
        # had this pid — it is gone.
        if not isinstance(pid, int) or pid == self._pid or not _pid_alive(pid):
            return "dead"
        hb = self._spool.read(f"o_{rec['owner']}", _HEARTBEAT_KIND)
        expires = hb.get("expires_at") if hb is not None else None
        if _finite(expires) and expires >= now and hb is not None and hb.get("pid") == pid:
            return "alive"
        return "silent"

    def owner_alive(self, rec: dict[str, Any]) -> bool:
        return self._owner_state(rec, time.time()) == "alive"

    def _records(self, *, include_done: bool = False) -> list[dict[str, Any]]:
        """Well-formed, unexpired records; finished ones only on request."""
        now = time.time()
        done = set() if include_done else self._spool.keys(_DONE_KIND)
        return [rec for key, rec in self._spool.list("rec")
                if _well_formed(key, rec) and key not in done and rec["expires_at"] >= now]

    def list_all(self) -> list[dict[str, Any]]:
        """Every pending record, annotated with `owner_alive`."""
        return [{**rec, "owner_alive": self.owner_alive(rec)} for rec in self._records()]

    def orphans(self) -> list[dict[str, Any]]:
        """Records whose owner is gone. Read-only: `claim` takes one over.

        A dead pid is enough. A silent owner (pid running, heartbeat stale) must
        be silent on two looks in a row: right after the machine wakes from
        sleep every heartbeat is stale, and the owners refresh theirs within a
        tick — handing their reminders away then would fire them twice.

        Finished (cancelled) records are included: `claim` is where an orphan
        cancelled while its owner was gone gets dropped.
        """
        now = time.time()
        out: list[dict[str, Any]] = []
        silent: set[str] = set()
        for rec in self._records(include_done=True):
            state = self._owner_state(rec, now)
            if state == "silent":
                silent.add(rec["owner"])
            if state == "dead" or (state == "silent" and rec["owner"] in self._silent):
                out.append(rec)
        self._silent = silent
        return out

    def claim(self, rec: dict[str, Any]) -> dict[str, Any] | None:
        """Take over an orphan. None if another instance won it, or if it was
        cancelled or already fired (the record is then dropped, not fired)."""
        rid = rec["rid"]
        claim_kind = "claim" + rec["owner"]
        now = time.time()
        if not self._spool.write_exclusive(
            rid, claim_kind, {"pid": self._pid, "at": now, "expires_at": rec["expires_at"]}
        ):
            return None
        # The claim file stays until it expires: removing it now would let an
        # instance still holding the old record win the same claim again.
        if self._is_finished(rid):
            log.info("reminder %s was cancelled or already fired; dropping it", rid)
            self.remove_record(rid)
            return None
        mine = {**rec, "pid": self._pid, "owner": self._instance}
        if not self._spool.write(rid, "rec", mine):
            # The record still names the old owner. Give the claim back so this
            # generation can be adopted again, instead of stranding it.
            self._spool.remove(rid, claim_kind)
            return None
        return mine

    def request_cancel(self, rid: str) -> int | None:
        """Cancel a reminder this process does not own. Returns the owner's
        pid, or None when no such pending reminder is on disk (or it already
        fired) or the request could not be written.

        The request is the record's `done` tombstone, so it lasts as long as the
        record could: an owner or adopter acting on it hours later still drops
        the reminder.
        """
        rec = self._spool.read(rid, "rec")
        if rec is None or not _well_formed(rid, rec):
            return None
        now = time.time()
        if not self._spool.write_exclusive(rid, _DONE_KIND, {
            "requested_by": self._pid, "at": now,
            "expires_at": max(rec["expires_at"], now + self._cancel_ttl_s),
        }):
            return None
        return rec["pid"]

    def finished(self, rids: list[str]) -> list[str]:
        """The given reminders that now have a tombstone (cancelled elsewhere)."""
        done = self._spool.keys(_DONE_KIND)
        return [rid for rid in rids if rid in done]

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
