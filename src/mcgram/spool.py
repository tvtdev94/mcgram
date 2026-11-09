"""Cross-process file spool: atomic JSON records under a namespaced directory.

Knows nothing about `ask` or `reminder` semantics — it stores, lists, and expires
small JSON payloads so separate mcgram processes can hand work to each other
without a daemon, a socket, or a new dependency.

Every I/O helper swallows `OSError` and returns a degraded value (`False` /
`None` / `0`) instead of raising: a spool failure must never surface as an
exception inside an MCP tool handler.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .spool_safety import probe_dir

log = logging.getLogger("mcgram.spool")

# Keys are minted by mcgram itself (`q_<hex>`, `r_<hex>`) or derived from a
# Telegram update id (`u<digits>`). Everything else — path separators, `..`,
# drive letters — is rejected before it can reach the filesystem.
# `fullmatch`, not `match`: `$` also matches before a trailing newline, which
# would make `q_ab\n` a second key that looks identical to `q_ab` in every log.
_KEY_RE = re.compile(r"[a-z]_[0-9a-f]{8,32}")
_UPDATE_KEY_RE = re.compile(r"u[0-9]{1,20}")
_KIND_RE = re.compile(r"[a-z]+")

# json.load can blow the stack on deeply nested input and json.dump raises
# TypeError on values it cannot serialize. Neither is an OSError, and both must
# degrade rather than surface inside an MCP tool handler.
_READ_ERRORS = (OSError, ValueError, UnicodeDecodeError, RecursionError)
_WRITE_ERRORS = (OSError, TypeError, ValueError, RecursionError)


def valid_key(key: str) -> bool:
    """True when `key` is safe to interpolate into a filename."""
    return bool(_KEY_RE.fullmatch(key) or _UPDATE_KEY_RE.fullmatch(key))


def new_key(prefix: str) -> str:
    """Mint a fresh key satisfying `valid_key`. `prefix` is a single lowercase letter."""
    if not prefix.isalpha() or not prefix.islower() or len(prefix) != 1:
        raise ValueError(f"key prefix must be one lowercase letter, got {prefix!r}")
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _load(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except _READ_ERRORS:
        return None
    return data if isinstance(data, dict) else None


def _unlink(path: Path) -> int:
    """Delete `path`, returning 1 if it actually went away."""
    try:
        path.unlink()
    except FileNotFoundError:
        return 0
    except OSError:
        log.debug("spool: could not unlink %s", path, exc_info=True)
        return 0
    return 1


class Spool:
    """A namespaced directory of atomic JSON records.

    Layout: `{root}/{namespace}/{key}.{kind}.json`. Two namespaces under the same
    root never see each other's records.
    """

    def __init__(self, root: str | Path, namespace: str) -> None:
        self._root = Path(root).expanduser()
        self._dir = self._root / namespace
        self.namespace = namespace

    @property
    def dir(self) -> Path:
        return self._dir

    def path_for(self, key: str, kind: str) -> Path | None:
        """Absolute path for a record, or None when key/kind is unsafe."""
        if not valid_key(key) or not _KIND_RE.fullmatch(kind):
            log.warning("spool: rejected key=%r kind=%r", key, kind)
            return None
        return self._dir / f"{key}.{kind}.json"

    def _ensure_dir(self) -> None:
        # `mkdir(parents=True, mode=...)` applies the mode to the leaf only, so
        # root and namespace are created separately to keep both at 0700.
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._dir.mkdir(exist_ok=True, mode=0o700)

    def write(self, key: str, kind: str, payload: dict[str, Any]) -> bool:
        """Atomically write a record, replacing any existing one.

        Readers never observe a half-written file: content goes to a PID-tagged
        temp file first, then `os.replace` (atomic on POSIX and Windows).
        """
        target = self.path_for(key, kind)
        if target is None:
            return False
        # uuid, not just PID: two threads in one process would otherwise collide
        # on the temp name and the second would see a spurious failure.
        tmp = target.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            self._ensure_dir()
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            log.debug("spool: temp create failed for %s", target, exc_info=True)
            return False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, allow_nan=False)
            os.replace(tmp, target)
            return True
        except _WRITE_ERRORS:
            log.debug("spool: write failed for %s", target, exc_info=True)
            return False
        finally:
            # After a successful replace the temp file is already gone; the
            # FileNotFoundError that raises is expected, not an error.
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    def write_exclusive(self, key: str, kind: str, payload: dict[str, Any]) -> bool:
        """Create a record only if it does not exist yet — the claim primitive.

        `O_EXCL` on the target itself (same pattern as `lock.py`) is what makes
        the claim race-free: the kernel lets exactly one of N racing processes
        create the file. Content is written in place rather than renamed in, so
        a reader can in principle catch a partial file — callers treat this
        record's *existence* as the signal and tolerate `read` returning None.
        `sweep` gives unparseable files a grace period for the same reason.
        """
        target = self.path_for(key, kind)
        if target is None:
            return False
        try:
            self._ensure_dir()
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        except OSError:
            log.debug("spool: claim failed for %s", target, exc_info=True)
            return False
        committed = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, allow_nan=False)
            committed = True
            return True
        except _WRITE_ERRORS:
            log.debug("spool: claim write failed for %s", target, exc_info=True)
            return False
        finally:
            # A half-written claim nobody owns would lock the key out until
            # sweep; drop it here whatever the failure was.
            if not committed:
                _unlink(target)

    def read(self, key: str, kind: str) -> dict[str, Any] | None:
        """Parsed record, or None when missing, unreadable, or malformed."""
        target = self.path_for(key, kind)
        return None if target is None else _load(target)

    def keys(self, kind: str) -> set[str]:
        """Record keys of `kind` — filenames only, no JSON parsing.

        Filtered through `valid_key` so a stray file cannot hand a caller a key
        that every later spool call would then silently reject.
        """
        if not _KIND_RE.fullmatch(kind):
            return set()
        suffix = f".{kind}.json"
        try:
            names = os.listdir(self._dir)
        except OSError:
            return set()
        found = (n[: -len(suffix)] for n in names if n.endswith(suffix))
        return {k for k in found if valid_key(k)}

    def list(self, kind: str) -> list[tuple[str, dict[str, Any]]]:
        """All readable records of `kind`, key-sorted. Malformed files are skipped."""
        out: list[tuple[str, dict[str, Any]]] = []
        for key in sorted(self.keys(kind)):
            rec = self.read(key, kind)
            if rec is not None:
                out.append((key, rec))
        return out

    def remove(self, key: str, *kinds: str) -> bool:
        """Delete the given kinds for `key`. False if anything survives.

        Callers must not assume the spool is clean on a False: a surviving
        answer file means its key cannot be reused.
        """
        ok = True
        for kind in kinds:
            target = self.path_for(key, kind)
            if target is None:
                ok = False
                continue
            try:
                target.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                log.warning("spool: could not remove %s", target, exc_info=True)
                ok = False
        return ok

    def sweep(self, now: float | None = None, *, tmp_grace_s: int = 300) -> int:
        """Delete expired records, records missing `expires_at`, and stale temp files.

        A record without `expires_at` is treated as corrupt rather than
        immortal. The alternative leaks it forever, and an answer that outlives
        its question can be picked up by a later one that reuses the key.

        Unparseable files get the same grace period as temp files. Without it,
        `write_exclusive` has a window where the claim exists but is still
        empty, and a sweep from any other process would delete it — on POSIX
        that unlink succeeds even while the owner holds the file open, so the
        claim silently vanishes and the request gets served twice.
        """
        now = time.time() if now is None else now
        removed = 0
        try:
            entries = list(os.scandir(self._dir))
        except OSError:
            return 0
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            path = Path(entry.path)
            if entry.name.endswith(".tmp"):
                if _mtime(entry) + tmp_grace_s < now:
                    removed += _unlink(path)
                continue
            if not entry.name.endswith(".json"):
                continue
            rec = _load(path)
            if rec is None:
                if _mtime(entry) + tmp_grace_s < now:
                    removed += _unlink(path)
                continue
            expires = rec.get("expires_at")
            if not _finite_number(expires):
                log.warning("spool: %s has no usable expires_at; sweeping as corrupt", entry.name)
                removed += _unlink(path)
            elif expires < now:
                removed += _unlink(path)
        return removed

    def probe(self) -> tuple[bool, str]:
        """(ok, reason) — is this spool safe to use? See `spool_safety`."""
        return probe_dir(self._root, self._dir)


def _mtime(entry: os.DirEntry[str]) -> float:
    try:
        return entry.stat().st_mtime
    except OSError:
        return 0.0


def _finite_number(value: Any) -> bool:
    """True for a real timestamp. `bool` is an int subclass, so exclude it.

    Infinity/NaN are rejected too: json accepts them, and either one produces a
    record sweep can never expire.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value)
