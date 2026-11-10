"""Shared `priority` / `title` / `tags` handling for the send tools.

These map to ntfy's Priority / Title / Tags headers. They are validated on every
transport (bad input is bad input), then dropped with a `note` on Telegram and
Discord, mirroring how `mention` reports being ignored off Discord.
"""

from __future__ import annotations

from typing import Any

from ..config import Destination

PRIORITY_MIN, PRIORITY_MAX = 1, 5

SCHEMA_PROPERTIES: dict[str, Any] = {
    "priority": {
        "type": "integer",
        "minimum": PRIORITY_MIN,
        "maximum": PRIORITY_MAX,
        "description": (
            "ntfy only: 1 (min) to 5 (max/urgent); 3 is ntfy's default. Overrides "
            "`silent`. Ignored on Telegram/Discord."
        ),
    },
    "title": {
        "type": "string",
        "description": "ntfy only: notification title. Ignored on Telegram/Discord.",
    },
    "tags": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "ntfy only: tags or emoji shortcodes (e.g. [\"warning\", \"rocket\"]). "
            "Ignored on Telegram/Discord."
        ),
    },
}


def resolve_ntfy_options(
    dest: Destination,
    *,
    priority: Any = None,
    title: Any = None,
    tags: Any = None,
) -> tuple[dict[str, Any], str | None, dict[str, Any] | None]:
    """Validate the options and decide whether they apply to `dest`.

    Returns `(kwargs, note, error)`: dispatch kwargs for ntfy (empty elsewhere),
    a note when options were given for a transport that ignores them, or an
    `invalid_input` error dict for the caller to return.
    """
    if priority is not None and (
        isinstance(priority, bool) or not isinstance(priority, int)
        or not PRIORITY_MIN <= priority <= PRIORITY_MAX
    ):
        return {}, None, {"error": "invalid_input", "reason": "invalid_priority",
                          "min": PRIORITY_MIN, "max": PRIORITY_MAX}
    if title is not None and not isinstance(title, str):
        return {}, None, {"error": "invalid_input", "reason": "invalid_title"}
    if tags is not None and (
        not isinstance(tags, list)
        or not all(isinstance(t, str) and t.strip() and "," not in t for t in tags)
    ):
        return {}, None, {"error": "invalid_input", "reason": "invalid_tags",
                          "detail": "tags must be a list of non-empty strings without commas"}
    given = {
        "priority": priority,
        "title": title or None,
        "tags": [t.strip() for t in tags] if tags else None,
    }
    if all(v is None for v in given.values()):
        return {}, None, None
    if dest.transport != "ntfy":
        return {}, f"priority/title/tags ignored for {dest.transport}", None
    return given, None, None
