"""Spool unit tests: atomicity, key validation, TTL sweep, and safety probe."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from mcgram.spool import Spool, new_key, valid_key

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes/uid only")


def _spool(tmp_path: Path, namespace: str = "asks") -> Spool:
    return Spool(tmp_path / "spool", namespace)


def _rec(**kw: object) -> dict[str, object]:
    """A record with the mandatory `expires_at`, far in the future by default."""
    return {"expires_at": time.time() + 300, **kw}


# --- round-trip -------------------------------------------------------------


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    assert s.write("q_abcdef0123456789", "request", _rec(question="deploy?")) is True
    got = s.read("q_abcdef0123456789", "request")
    assert got is not None
    assert got["question"] == "deploy?"


def test_write_replaces_existing_record(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    s.write("q_abcdef0123456789", "request", _rec(question="first"))
    s.write("q_abcdef0123456789", "request", _rec(question="second"))
    got = s.read("q_abcdef0123456789", "request")
    assert got is not None and got["question"] == "second"


def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert _spool(tmp_path).read("q_abcdef0123456789", "answer") is None


def test_read_corrupt_json_returns_none_without_raising(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    s.write("q_abcdef0123456789", "request", _rec())
    path = s.path_for("q_abcdef0123456789", "request")
    assert path is not None
    path.write_text("{not json", encoding="utf-8")
    assert s.read("q_abcdef0123456789", "request") is None


def test_read_non_dict_json_returns_none(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    s.write("q_abcdef0123456789", "request", _rec())
    path = s.path_for("q_abcdef0123456789", "request")
    assert path is not None
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert s.read("q_abcdef0123456789", "request") is None


# --- exclusive claim --------------------------------------------------------


def test_write_exclusive_second_call_fails_and_keeps_original(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    assert s.write_exclusive("q_abcdef0123456789", "taken", _rec(pid=111)) is True
    assert s.write_exclusive("q_abcdef0123456789", "taken", _rec(pid=222)) is False
    got = s.read("q_abcdef0123456789", "taken")
    assert got is not None and got["pid"] == 111


def test_write_exclusive_grants_exactly_one_winner_under_contention(tmp_path: Path) -> None:
    """The claim primitive is what stops a request being served twice, so the
    concurrent case is the one that matters."""
    s = _spool(tmp_path)
    s.write("q_abcdef0123456789", "request", _rec())  # create dir up front
    start = threading.Barrier(8)
    results: list[bool] = []
    lock = threading.Lock()

    def claim(n: int) -> None:
        start.wait()
        won = s.write_exclusive("q_abcdef0123456789", "taken", _rec(pid=n))
        with lock:
            results.append(won)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 1
    assert s.read("q_abcdef0123456789", "taken") is not None


def test_write_exclusive_unserializable_payload_does_not_lock_the_key(tmp_path: Path) -> None:
    """A failed claim must release the key, not leave a half-written file that
    no owner holds and no other owner can take."""
    s = _spool(tmp_path)
    assert s.write_exclusive("q_abcdef0123456789", "taken", _rec(when=object())) is False
    assert s.keys("taken") == set()
    assert s.write_exclusive("q_abcdef0123456789", "taken", _rec(pid=7)) is True


def test_concurrent_writes_never_leave_a_torn_record(tmp_path: Path) -> None:
    """`write` guarantees atomicity, not that every concurrent writer wins.

    On Windows `os.replace` fails while another thread is swapping the same
    target; that path degrades to False by design (no retry loop). What must
    hold either way: the record on disk is always one writer's complete
    payload, never a mix, and no temp files are left behind.
    """
    s = _spool(tmp_path)
    s.write("q_abcdef0123456789", "request", _rec(n=-1))
    start = threading.Barrier(6)
    results: list[bool] = []
    lock = threading.Lock()

    def write(n: int) -> None:
        start.wait()
        ok = s.write("q_abcdef0123456789", "request", _rec(n=n))
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert any(results)
    got = s.read("q_abcdef0123456789", "request")
    assert got is not None
    assert got["n"] in range(-1, 6)
    assert [p.name for p in s.dir.iterdir() if p.name.endswith(".tmp")] == []


# --- listing ----------------------------------------------------------------


def test_list_skips_corrupt_files(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    s.write("q_aaaaaaaa11111111", "request", _rec(n=1))
    s.write("q_bbbbbbbb22222222", "request", _rec(n=2))
    bad = s.path_for("q_bbbbbbbb22222222", "request")
    assert bad is not None
    bad.write_text("garbage", encoding="utf-8")
    listed = s.list("request")
    assert [k for k, _ in listed] == ["q_aaaaaaaa11111111"]


def test_keys_returns_names_without_parsing(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    s.write("q_aaaaaaaa11111111", "request", _rec())
    s.write("q_bbbbbbbb22222222", "request", _rec())
    s.write("q_aaaaaaaa11111111", "answer", _rec())
    bad = s.path_for("q_bbbbbbbb22222222", "request")
    assert bad is not None
    bad.write_text("garbage", encoding="utf-8")
    # Corrupt content still shows up in keys() — it is a filename-level view.
    assert s.keys("request") == {"q_aaaaaaaa11111111", "q_bbbbbbbb22222222"}
    assert s.keys("answer") == {"q_aaaaaaaa11111111"}


def test_keys_on_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert _spool(tmp_path).keys("request") == set()


def test_keys_filters_out_names_that_are_not_valid_keys(tmp_path: Path) -> None:
    """Returning a key that every later call would reject makes a caller unable
    to tell "claimed by someone else" from "this was never a key"."""
    s = _spool(tmp_path)
    s.write("q_abcdef0123456789", "request", _rec())
    (s.dir / ".request.json").write_text("{}", encoding="utf-8")
    (s.dir / "NOT-A-KEY.request.json").write_text("{}", encoding="utf-8")
    assert s.keys("request") == {"q_abcdef0123456789"}
    assert [k for k, _ in s.list("request")] == ["q_abcdef0123456789"]


def test_namespaces_are_isolated(tmp_path: Path) -> None:
    asks = _spool(tmp_path, "asks")
    reminders = _spool(tmp_path, "reminders")
    asks.write("q_abcdef0123456789", "request", _rec())
    assert reminders.keys("request") == set()
    assert reminders.read("q_abcdef0123456789", "request") is None


# --- removal ----------------------------------------------------------------


def test_remove_returns_true_when_all_gone(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    s.write("q_abcdef0123456789", "request", _rec())
    s.write("q_abcdef0123456789", "answer", _rec())
    assert s.remove("q_abcdef0123456789", "request", "answer") is True
    assert s.keys("request") == set()
    assert s.keys("answer") == set()


def test_remove_missing_file_is_not_a_failure(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    assert s.remove("q_abcdef0123456789", "request") is True


def test_remove_returns_false_on_invalid_key(tmp_path: Path) -> None:
    assert _spool(tmp_path).remove("../evil", "request") is False


def test_remove_returns_false_when_unlink_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _spool(tmp_path)
    s.write("q_abcdef0123456789", "request", _rec())

    def boom(self: Path) -> None:
        raise PermissionError("locked by antivirus")

    monkeypatch.setattr(Path, "unlink", boom)
    assert s.remove("q_abcdef0123456789", "request") is False


# --- sweep ------------------------------------------------------------------


def test_sweep_removes_expired_records(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    now = time.time()
    s.write("q_aaaaaaaa11111111", "request", {"expires_at": now - 1})
    s.write("q_bbbbbbbb22222222", "request", {"expires_at": now + 300})
    assert s.sweep(now) == 1
    assert s.keys("request") == {"q_bbbbbbbb22222222"}


def test_sweep_removes_records_missing_expires_at(tmp_path: Path) -> None:
    """A record without expires_at is corrupt, not immortal.

    Leaving it forever means an answer can outlive its question and be picked
    up by a later ask that reuses the key.
    """
    s = _spool(tmp_path)
    s.write("q_aaaaaaaa11111111", "answer", {"value": "Approve"})
    assert s.sweep(time.time()) == 1
    assert s.keys("answer") == set()


def test_sweep_removes_record_with_non_numeric_expires_at(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    s.write("q_aaaaaaaa11111111", "answer", {"expires_at": "soon"})
    assert s.sweep(time.time()) == 1


def test_sweep_removes_corrupt_json_only_after_grace(tmp_path: Path) -> None:
    """Corrupt files wait out the grace period like temp files do.

    A fresh unparseable file is more likely a `write_exclusive` claim caught
    mid-write than real corruption — see the claim/sweep race test below.
    """
    s = _spool(tmp_path)
    s.write("q_aaaaaaaa11111111", "request", _rec())
    path = s.path_for("q_aaaaaaaa11111111", "request")
    assert path is not None
    path.write_text("{broken", encoding="utf-8")
    now = time.time()
    assert s.sweep(now, tmp_grace_s=300) == 0
    assert path.exists()
    os.utime(path, (now - 1000, now - 1000))
    assert s.sweep(now, tmp_grace_s=300) == 1


def test_sweep_does_not_delete_a_claim_being_written(tmp_path: Path) -> None:
    """An in-flight claim must survive a concurrent sweep.

    `write_exclusive` creates the file with O_EXCL and fills it after, so there
    is a window where it parses as nothing. On POSIX an unlink during that
    window succeeds even with the file open: the owner's write then goes
    nowhere, the claim disappears, and the next scan serves the same request a
    second time.
    """
    s = _spool(tmp_path)
    s.write("q_aaaaaaaa11111111", "request", _rec())
    claim = s.dir / "q_aaaaaaaa11111111.taken.json"
    claim.touch()  # 0 bytes, exactly what O_EXCL leaves before the dump
    assert s.sweep(time.time()) == 0
    assert claim.exists()
    # The claim is real once written, and still not swept.
    s.write_exclusive("q_bbbbbbbb22222222", "taken", _rec(pid=1))
    assert s.sweep(time.time()) == 0
    assert s.read("q_bbbbbbbb22222222", "taken") is not None


def test_sweep_removes_record_with_infinite_expires_at(tmp_path: Path) -> None:
    """json accepts Infinity/NaN; either would make a record unexpirable."""
    s = _spool(tmp_path)
    for key, raw in (
        ("q_aaaaaaaa11111111", '{"expires_at": Infinity}'),
        ("q_bbbbbbbb22222222", '{"expires_at": NaN}'),
    ):
        path = s.path_for(key, "answer")
        assert path is not None
        s.write(key, "answer", _rec())
        path.write_text(raw, encoding="utf-8")
    assert s.sweep(time.time()) == 2


def test_sweep_removes_record_with_bool_expires_at(tmp_path: Path) -> None:
    """`True < now` is valid Python; treating bool as a timestamp would delete
    or keep records by accident."""
    s = _spool(tmp_path)
    s.write("q_aaaaaaaa11111111", "answer", {"expires_at": True})
    assert s.sweep(time.time()) == 1


def test_sweep_removes_stale_tmp_but_keeps_fresh(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    s.write("q_aaaaaaaa11111111", "request", _rec())
    stale = s.dir / "q_cccccccc33333333.request.999.tmp"
    fresh = s.dir / "q_dddddddd44444444.request.998.tmp"
    stale.write_text("{}", encoding="utf-8")
    fresh.write_text("{}", encoding="utf-8")
    now = time.time()
    os.utime(stale, (now - 1000, now - 1000))
    assert s.sweep(now, tmp_grace_s=300) == 1
    assert not stale.exists()
    assert fresh.exists()


def test_sweep_on_missing_dir_returns_zero(tmp_path: Path) -> None:
    assert _spool(tmp_path).sweep(time.time()) == 0


# --- key validation ---------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "../x",
        "a/b",
        "C:evil",
        "q_XYZ",
        "",
        "q_short",
        "..",
        "q_abcdef0123456789/../x",
        # `$` in a Python regex also matches before a trailing newline, so these
        # would sneak past a `match`-based check and create a second key that
        # is indistinguishable from the first in any log line.
        "q_abcdef0123456789\n",
        "u12345\n",
    ],
)
def test_invalid_keys_are_rejected(key: str) -> None:
    assert valid_key(key) is False


@pytest.mark.parametrize("key", ["q_abcdef0123456789", "r_0123456789abcdef", "u12345"])
def test_valid_keys_are_accepted(key: str) -> None:
    assert valid_key(key) is True


def test_new_key_is_valid() -> None:
    assert valid_key(new_key("q")) is True
    assert valid_key(new_key("r")) is True


@pytest.mark.parametrize("prefix", ["ask", "Q", "", "q_"])
def test_new_key_rejects_bad_prefix(prefix: str) -> None:
    """Minting an unusable key would make every later spool call fail silently."""
    with pytest.raises(ValueError):
        new_key(prefix)


@pytest.mark.parametrize("key", ["../x", "a/b", "C:evil", "q_XYZ", ""])
def test_invalid_key_touches_no_file_outside_namespace(tmp_path: Path, key: str) -> None:
    """Traversal must fail before the filesystem is touched, not after."""
    s = _spool(tmp_path)
    s.write("q_aaaaaaaa11111111", "request", _rec())  # create the namespace dir
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")
    before = sorted(p.name for p in (tmp_path / "spool" / "asks").iterdir())

    assert s.path_for(key, "request") is None
    assert s.write(key, "request", _rec()) is False
    assert s.write_exclusive(key, "taken", _rec()) is False
    assert s.read(key, "request") is None
    assert s.remove(key, "request") is False

    assert outside.read_text(encoding="utf-8") == "untouched"
    assert sorted(p.name for p in (tmp_path / "spool" / "asks").iterdir()) == before


@pytest.mark.parametrize("kind", ["../evil", "re/quest", "", "Request", "request\n"])
def test_invalid_kind_is_rejected(tmp_path: Path, kind: str) -> None:
    s = _spool(tmp_path)
    assert s.path_for("q_abcdef0123456789", kind) is None
    assert s.write("q_abcdef0123456789", kind, _rec()) is False
    assert s.keys(kind) == set()


# --- modes ------------------------------------------------------------------


@POSIX_ONLY
def test_dir_is_0700_and_file_is_0600(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    s.write("q_abcdef0123456789", "request", _rec())
    path = s.path_for("q_abcdef0123456789", "request")
    assert path is not None
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (s.dir.stat().st_mode & 0o777) == 0o700
    assert ((tmp_path / "spool").stat().st_mode & 0o777) == 0o700


@POSIX_ONLY
def test_exclusive_claim_file_is_0600(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    s.write_exclusive("q_abcdef0123456789", "taken", _rec())
    path = s.path_for("q_abcdef0123456789", "taken")
    assert path is not None
    assert (path.stat().st_mode & 0o777) == 0o600


# --- probe ------------------------------------------------------------------


def test_probe_ok_on_fresh_dir(tmp_path: Path) -> None:
    ok, reason = _spool(tmp_path).probe()
    assert ok is True
    assert reason == "ok"


def test_probe_leaves_no_residue(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    s.probe()
    assert list(s.dir.iterdir()) == []


@POSIX_ONLY
def test_probe_rejects_symlinked_dir(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "spool"
    link.symlink_to(real, target_is_directory=True)
    ok, reason = Spool(link, "asks").probe()
    assert ok is False
    assert "symlink" in reason


@POSIX_ONLY
def test_probe_rejects_world_writable_dir(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    (root / "asks").mkdir(parents=True)
    (root / "asks").chmod(0o777)
    ok, reason = Spool(root, "asks").probe()
    assert ok is False
    assert "wider than 0700" in reason


@POSIX_ONLY
def test_probe_rejects_world_writable_root_even_when_leaf_is_tight(tmp_path: Path) -> None:
    """Permission to replace a directory entry comes from its parent, so a 0700
    leaf inside a 0777 root is still swappable by any local user."""
    root = tmp_path / "spool"
    (root / "asks").mkdir(parents=True)
    (root / "asks").chmod(0o700)
    root.chmod(0o777)
    try:
        ok, reason = Spool(root, "asks").probe()
        assert ok is False
        assert "wider than 0700" in reason
    finally:
        root.chmod(0o700)


@POSIX_ONLY
@pytest.mark.skipif(
    hasattr(os, "getuid") and os.getuid() == 0, reason="root bypasses mode bits"
)
def test_probe_rejects_unwritable_root(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    root.chmod(0o500)
    try:
        ok, reason = Spool(root, "asks").probe()
        assert ok is False
        assert reason != "ok"
    finally:
        root.chmod(0o700)


def test_probe_fails_when_root_path_is_a_file(tmp_path: Path) -> None:
    blocker = tmp_path / "spool"
    blocker.write_text("not a dir", encoding="utf-8")
    ok, reason = Spool(blocker, "asks").probe()
    assert ok is False
    assert reason != "ok"


# --- degradation ------------------------------------------------------------


def test_write_returns_false_when_dir_cannot_be_created(tmp_path: Path) -> None:
    blocker = tmp_path / "spool"
    blocker.write_text("not a dir", encoding="utf-8")
    s = Spool(blocker, "asks")
    assert s.write("q_abcdef0123456789", "request", _rec()) is False
    assert s.write_exclusive("q_abcdef0123456789", "taken", _rec()) is False


def test_write_leaves_no_tmp_file_behind(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    s.write("q_abcdef0123456789", "request", _rec())
    assert [p.name for p in s.dir.iterdir() if p.name.endswith(".tmp")] == []


def test_write_failure_cleans_up_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _spool(tmp_path)
    s.write("q_aaaaaaaa11111111", "request", _rec())  # ensure dir exists

    def boom(src: object, dst: object) -> None:
        raise OSError("replace blocked")

    monkeypatch.setattr(os, "replace", boom)
    assert s.write("q_bbbbbbbb22222222", "request", _rec()) is False
    assert [p.name for p in s.dir.iterdir() if p.name.endswith(".tmp")] == []


def test_write_returns_false_for_unserializable_payload(tmp_path: Path) -> None:
    """A TypeError from json.dump must degrade, not escape into a tool handler."""
    s = _spool(tmp_path)
    assert s.write("q_abcdef0123456789", "request", _rec(when=object())) is False
    assert [p.name for p in s.dir.iterdir() if p.name.endswith(".tmp")] == []
    assert s.keys("request") == set()


def test_read_and_sweep_survive_deeply_nested_json(tmp_path: Path) -> None:
    """json.load raises RecursionError, which is not an OSError. Letting it
    escape would kill the sweeper task and leak the whole spool."""
    s = _spool(tmp_path)
    s.write("q_abcdef0123456789", "request", _rec())
    path = s.path_for("q_abcdef0123456789", "request")
    assert path is not None
    path.write_text("[" * 200_000, encoding="utf-8")
    assert s.read("q_abcdef0123456789", "request") is None
    assert s.sweep(time.time()) == 0  # unparseable, still inside grace


def test_write_rejects_nan_rather_than_writing_invalid_json(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    assert s.write("q_abcdef0123456789", "request", {"expires_at": float("inf")}) is False


def test_record_written_is_valid_json_on_disk(tmp_path: Path) -> None:
    s = _spool(tmp_path)
    s.write("q_abcdef0123456789", "request", _rec(question="hi", options=["a", "b"]))
    path = s.path_for("q_abcdef0123456789", "request")
    assert path is not None
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert parsed["options"] == ["a", "b"]
