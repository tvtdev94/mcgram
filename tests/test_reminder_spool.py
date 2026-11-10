"""ReminderSpool: reminder records on disk, ownership, and orphan adoption.

Orphans are made the honest way — a separate Python process publishes the
record and exits — so "owner is dead" is a real dead pid with a foreign owner
token, not a patched liveness check.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mcgram.reminder_spool import ReminderSpool
from mcgram.spool import Spool

_REPO_SRC = Path(__file__).resolve().parent.parent / "src"

_PUBLISH = (
    "import sys\n"
    "from mcgram.spool import Spool\n"
    "from mcgram.reminder_spool import ReminderSpool\n"
    "store = ReminderSpool(Spool(sys.argv[1], 'reminders'), cancel_ttl_s=300)\n"
    "for rid, fires_at in zip(sys.argv[2::2], sys.argv[3::2]):\n"
    "    assert store.publish(rid, text='from ' + rid, fires_at=float(fires_at),\n"
    "                         channel='default', transport='telegram')\n"
)


def publish_from_dead_process(spool_root: Path, *reminders: tuple[str, float]) -> None:
    """Publish reminders from a process that has exited by the time this returns."""
    args = [str(x) for pair in reminders for x in pair]
    env = {**os.environ, "PYTHONPATH": str(_REPO_SRC)}
    subprocess.run([sys.executable, "-c", _PUBLISH, str(spool_root), *args],
                   check=True, env=env, timeout=60)


def _store(root: Path) -> ReminderSpool:
    return ReminderSpool(Spool(root, "reminders"), cancel_ttl_s=300)


def _publish(store: ReminderSpool, rid: str, fires_at: float | None = None) -> None:
    assert store.publish(rid, text="t", fires_at=fires_at or time.time() + 60,
                         channel="default", transport="telegram")


def test_own_record_is_listed_alive_and_finish_removes_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _publish(store, "r_0000aaaa")
    [rec] = store.list_all()
    assert (rec["rid"], rec["owner_alive"], rec["pid"]) == ("r_0000aaaa", True, os.getpid())
    assert store.orphans() == []
    assert store.finish("r_0000aaaa", rec["fires_at"]) is True
    assert store.list_all() == []


def test_record_of_an_exited_process_is_an_orphan(tmp_path: Path) -> None:
    publish_from_dead_process(tmp_path, ("r_0000bbbb", time.time() + 60))
    store = _store(tmp_path)
    [rec] = store.list_all()
    assert rec["owner_alive"] is False
    assert [o["rid"] for o in store.orphans()] == ["r_0000bbbb"]


def test_only_one_of_two_racing_adopters_wins_an_orphan(tmp_path: Path) -> None:
    publish_from_dead_process(tmp_path, ("r_0000cccc", time.time() + 60))
    first, second = _store(tmp_path), _store(tmp_path)
    seen_by_first, seen_by_second = first.orphans(), second.orphans()  # both saw it
    won = [first.claim(seen_by_first[0]), second.claim(seen_by_second[0])]
    assert sum(r is not None for r in won) == 1
    # The record now belongs to the winner, so nobody sees an orphan any more.
    assert first.orphans() == [] and second.orphans() == []
    rec = Spool(tmp_path, "reminders").read("r_0000cccc", "rec")
    assert rec is not None and rec["pid"] == os.getpid()


def test_orphan_cancelled_after_it_was_seen_is_dropped_not_adopted(tmp_path: Path) -> None:
    publish_from_dead_process(tmp_path, ("r_0000dddd", time.time() + 60))
    store = _store(tmp_path)
    [orphan] = store.orphans()
    assert store.request_cancel("r_0000dddd") is not None
    assert store.claim(orphan) is None
    assert store.list_all() == []
    assert Spool(tmp_path, "reminders").keys("rec") == set()


def test_cancel_marks_only_that_reminder_and_only_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _publish(store, "r_0000eeee")
    _publish(store, "r_0000ffff")
    assert store.request_cancel("r_0000eeee") == os.getpid()
    assert store.finished(["r_0000eeee", "r_0000ffff"]) == ["r_0000eeee"]
    assert store.request_cancel("r_0000eeee") is None  # already cancelled


def test_cancel_request_for_an_unknown_reminder_writes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.request_cancel("r_00001111") is None
    assert Spool(tmp_path, "reminders").keys("cancel") == set()


@pytest.mark.parametrize(
    "tamper",
    [
        {"owner": "../../evil"},  # would escape the claim kind into a path
        {"rid": "r_99999999"},  # record filed under a different key
        {"fires_at": "soon"},
        {"text": ""},
        {"pid": True},
    ],
    ids=["owner-token", "rid-mismatch", "fires_at", "empty-text", "bool-pid"],
)
def test_tampered_records_are_never_listed_or_adopted(tmp_path: Path, tamper: dict) -> None:
    store = _store(tmp_path)
    _publish(store, "r_0000abcd")
    spool = Spool(tmp_path, "reminders")
    rec = spool.read("r_0000abcd", "rec")
    assert rec is not None
    spool.write("r_0000abcd", "rec", {**rec, "pid": 2**22 + 7, **tamper})
    assert store.list_all() == []
    assert store.orphans() == []


@pytest.mark.parametrize("beating", [True, False], ids=["heartbeat-fresh", "heartbeat-gone"])
def test_owner_is_judged_by_its_heartbeat_not_only_its_pid(tmp_path: Path, beating: bool) -> None:
    """Windows reuses pids: a dead owner's pid can belong to an unrelated live
    process, and its reminders would then never fire. A silent owner is only
    taken for dead on a second look, so one stalled tick (or waking from
    sleep) does not hand a live session's reminders away."""
    stranger = _store(tmp_path)
    stranger._pid, stranger._instance = os.getppid(), "feedfacecafebeef"  # alive, not us
    _publish(stranger, "r_0000f00d")
    if not beating:
        Spool(tmp_path, "reminders").sweep(now=time.time() + 600)  # its heartbeat expired
    store = _store(tmp_path)
    assert store.orphans() == []
    assert [o["rid"] for o in store.orphans()] == ([] if beating else ["r_0000f00d"])


def test_tombstone_still_being_written_already_stops_the_fire(tmp_path: Path) -> None:
    # write_exclusive creates the file before filling it, so a reader can find
    # it empty; its existence alone must count as "cancelled or fired".
    store = _store(tmp_path)
    _publish(store, "r_0000beef")
    done = Spool(tmp_path, "reminders").path_for("r_0000beef", "done")
    assert done is not None
    done.write_bytes(b"")
    assert store.still_mine("r_0000beef") is False
