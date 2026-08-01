"""Atomic, permission-preserving upserts to the ~/.mcgram/.env credential file.

The .env file holds secrets (e.g. MCGRAM_BOT_TOKEN, Discord webhook URLs). Writes
must never truncate or drop unrelated lines, and must survive an interrupted
write — hence temp file + os.replace (atomic) and a 0o600 clamp.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def upsert_env_var(path: Path, key: str, value: str) -> None:
    """Set `key=value` in the dotenv file at `path`, creating it if needed.

    Updates an existing `key=` line in place; otherwise appends. Every other
    line (comments, other keys) is left byte-for-byte intact. The write is
    atomic and the result is clamped to 0o600 (best-effort on Windows).
    """
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()

    new_line = f"{key}={value}"
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)

    content = "\n".join(lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _chmod_600(path)


def _chmod_600(path: Path) -> None:
    """Restrict the file to the owner. No-op where the platform can't honor it."""
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
