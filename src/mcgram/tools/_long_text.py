"""Deliver text that is over a transport's message cap as a `.txt` attachment.

One notification, full text intact: the file holds the whole text and the
caption shows its beginning. mcgram writes the file itself into a private
temporary directory, so it never goes through `send_file`'s CWD check (that
check guards user-supplied paths) and is always deleted afterwards.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

FILENAME = "message.txt"


def truncate_caption(caption: str, max_chars: int) -> str:
    """Cut `caption` to `max_chars`, marking the cut with an ellipsis."""
    if len(caption) <= max_chars:
        return caption
    return caption[: max_chars - 1] + "…"


@contextmanager
def text_attachment(text: str) -> Iterator[Path]:
    """Yield the path of a UTF-8 `message.txt` holding `text`; delete on exit.

    `TemporaryDirectory` is created owner-only (mkdtemp), so other local users
    cannot read the text while it uploads. Cleanup errors are ignored: by then
    the message is delivered (or failed) and that outcome must win.
    """
    with tempfile.TemporaryDirectory(prefix="mcgram-", ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / FILENAME
        path.write_bytes(text.encode("utf-8"))
        yield path
