"""Single-instance PID-file lock (cross-platform: Windows + POSIX)."""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path
from types import TracebackType

from .errors import LockHeldError

#: `acquire` creates the lock file and writes the pid as two separate syscalls,
#: so a reader can catch the file existing but still empty. These bound how long
#: we wait for that write to land before calling the lock stale.
_PID_WRITE_GRACE_S = 0.05
_PID_WRITE_TRIES = 10
_ACQUIRE_MAX_ATTEMPTS = 20


def _pid_alive(pid: int) -> bool:
    """Return True if a process with `pid` is currently running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    return _pid_alive_posix(pid)


def _pid_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def _pid_alive_windows(pid: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0:
            return False
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class SingleInstanceLock:
    """Acquire an exclusive PID file. Raises LockHeldError if another live process holds it.

    Stale lock files (owner PID no longer running) are auto-cleaned.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._acquired = False

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def acquire(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        my_pid = os.getpid()
        for _ in range(_ACQUIRE_MAX_ATTEMPTS):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                held_pid = _read_pid_settled(self.path)
                if held_pid is not None and _pid_alive(held_pid):
                    raise LockHeldError(held_pid, str(self.path)) from None
                # Stale → unlink and retry.
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                except PermissionError:
                    # Windows refuses to unlink a file another process still
                    # holds open — so the lock is live after all, its owner just
                    # hasn't written the pid yet. Never delete it.
                    raise LockHeldError(held_pid or 0, str(self.path)) from None
                continue
            with os.fdopen(fd, "w") as f:
                f.write(str(my_pid))
            self._acquired = True
            return my_pid
        # Contended every attempt: someone holds it, we just never read a pid
        # out of it. pid 0 renders as "another mcgram instance", no pid quoted.
        held = _read_pid(self.path)
        raise LockHeldError(held if held is not None else 0, str(self.path))

    def release(self) -> None:
        if not self._acquired:
            return
        with contextlib.suppress(FileNotFoundError, PermissionError):
            self.path.unlink()
        self._acquired = False


def _read_pid_settled(path: Path) -> int | None:
    """Read the owner pid, tolerating the gap between create and pid write.

    A reader that catches the file empty must not conclude "stale" — unlinking
    then deletes a live owner's lock, and two processes end up believing they
    own it. Wait for the write to land before deciding.
    """
    for _ in range(_PID_WRITE_TRIES):
        pid = _read_pid(path)
        if pid is not None:
            return pid
        time.sleep(_PID_WRITE_GRACE_S)
    return _read_pid(path)


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return int(raw)
    except ValueError:
        return None
