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


def probe_dir(root: Path, leaf: Path) -> tuple[bool, str]:
    """(ok, reason) for a spool root and its namespace directory.

    Windows skips the uid/mode checks — NTFS ACLs on the user profile carry that
    guarantee instead — but still rejects symlinks and unwritable paths.
    """
    for path in (root, leaf):
        if path.is_symlink():
            return False, f"{path} is a symlink"
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        leaf.mkdir(exist_ok=True, mode=0o700)
    except OSError as e:
        return False, f"cannot create {leaf}: {e}"
    if _POSIX:
        # Both levels matter: the right to replace a directory entry comes from
        # write permission on its *parent*, so a 0700 leaf inside a 0777 root is
        # still swappable by any local user.
        for path in (root, leaf):
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
