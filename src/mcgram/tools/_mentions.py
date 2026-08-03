"""Shared mention-resolution helper for the Discord send tools.

Keeps the `mention=[...]` handling identical across send_message / send_file /
send_video: resolve registered names to user IDs, or surface a structured error
(unknown name) / note (non-Discord transport, where mentions are ignored).
"""

from __future__ import annotations

from typing import Any

from ..config import Destination
from ..discord_client import resolve_mentions


def resolve_for_dest(
    dest: Destination, mention: list[str] | None
) -> tuple[list[str], str | None, dict[str, Any] | None]:
    """Resolve mention names against a destination.

    Returns `(ids, note, error)`:
    - no `mention` requested → `([], None, None)`
    - non-Discord transport → `([], "mention ignored for <t>", None)`
    - Discord, all names known → `(ids, None, None)`
    - Discord, some names unknown → `([], None, {error dict})` (caller returns it)
    """
    if not mention:
        return [], None, None
    if dest.transport != "discord":
        return [], f"mention ignored for {dest.transport}", None
    registry = dest.discord_mentions or {}
    ids, unknown = resolve_mentions(mention, registry)
    if unknown:
        return [], None, {
            "error": "invalid_input",
            "reason": "unknown_mention",
            "unknown": unknown,
            "known": sorted(registry),
        }
    return ids, None, None
