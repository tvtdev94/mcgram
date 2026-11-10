"""Is a spool directory *safe* to write into — not merely writable?

Writability alone is the dangerous test. `/tmp/mcgram/spool` passes it, and a
forged answer file there escalates broker spoofing from "same uid" to "anyone on
this machine". These checks run once at boot so the caller can log a concrete
reason for refusing to enable the broker.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path

log = logging.getLogger("mcgram.spool")

_POSIX = sys.platform != "win32"


def make_private_dirs(path: Path) -> None:
    """Create `path` and every missing directory above it, each at 0700.

    `mkdir(parents=True, mode=...)` applies the mode to the last level only and
    leaves the ones above it at the umask default (usually 0755), which the
    POSIX check below then rejects. So every missing level is made one at a time.
    """
    missing = []
    for level in (path, *path.parents):
        if level.exists():
            break
        missing.append(level)
    for level in reversed(missing):
        level.mkdir(exist_ok=True, mode=0o700)


def probe_dir(root: Path, leaf: Path) -> tuple[bool, str]:
    """(ok, reason) for a spool root and every directory down to `leaf`.

    `leaf` must lie under `root`; each level in between (a per-config directory,
    say) is created and checked exactly like the two ends.

    Windows skips the uid/mode checks — NTFS ACLs on the user profile carry that
    guarantee instead — but still rejects symlinks and unwritable paths.
    """
    try:
        parts = leaf.relative_to(root).parts
    except ValueError:
        return False, f"{leaf} is not inside {root}"
    levels = [root]
    for part in parts:
        levels.append(levels[-1] / part)
    for path in levels:
        if path.is_symlink():
            return False, f"{path} is a symlink"
    try:
        make_private_dirs(leaf)
    except OSError as e:
        return False, f"cannot create {leaf}: {e}"
    if _POSIX:
        # Every level matters: the right to replace a directory entry comes from
        # write permission on its *parent*, so a 0700 leaf inside a 0777 parent
        # is still swappable by any local user.
        for path in levels:
            ok, reason = _check_posix_ownership(path)
            if not ok:
                return False, reason
    return _check_writable(leaf)


def _check_posix_ownership(path: Path) -> tuple[bool, str]:
    """Reject a directory owned by another user or reachable beyond this one."""
    try:
        st = path.stat()
    except OSError as e:
        return False, f"cannot stat {path}: {e}"
    uid = os.getuid()
    if st.st_uid != uid:
        return False, f"{path} is owned by uid {st.st_uid}, not {uid}"
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return False, f"{path} mode {stat.S_IMODE(st.st_mode):04o} is wider than 0700"
    return True, "ok"


def _check_writable(path: Path) -> tuple[bool, str]:
    probe = path / ".probe"
    try:
        os.close(os.open(probe, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600))
    except OSError as e:
        return False, f"{path} is not writable: {e}"
    finally:
        try:
            probe.unlink()
        except OSError:
            log.debug("spool: could not clean probe file %s", probe, exc_info=True)
    return True, "ok"
