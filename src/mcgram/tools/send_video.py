"""Tool: send_video — upload a video that plays in-chat."""

from __future__ import annotations

import os
import time
from typing import Any

from ..discord_client import format_mention_prefix, webhook_id_from_url
from ..dispatch import send_video_file
from ..errors import (
    ConfigError,
    DiscordError,
    NtfyError,
    RateLimitError,
    TelegramError,
    TransportError,
)
from ..runtime import AppState
from . import _ntfy_options
from ._long_text import truncate_caption
from ._mentions import resolve_for_dest
from .send_file import _validate_path, file_cap

TOOL_NAME = "send_video"
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Upload a video to a configured channel. On Telegram, plays in-chat with "
            "thumbnail and scrubber (sendVideo endpoint). On ntfy.sh, the mobile app "
            "inline-plays video/* MIME. Use for .mp4/.mov/.mkv/.webm/.m4v. "
            "Path is CWD-restricted unless config sets allow_outside_cwd: true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to video file"},
                "channel": {
                    "type": "string",
                    "description": "Named channel from config (default: 'default')",
                },
                "caption": {"type": "string", "description": "Optional caption (≤1024 chars)"},
                "silent": {"type": "boolean", "default": False},
                "supports_streaming": {
                    "type": "boolean", "default": True,
                    "description": "Telegram-only: hint that the file is streamable",
                },
                "thread_id": {
                    "type": "string",
                    "description": (
                        "Discord only: ID of an existing thread to post into. Omit "
                        "to post to the base channel. Telegram/ntfy ignore it."
                    ),
                },
                "mention": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Discord only: names registered via "
                        "`mcgram discord mention add`. Prepends @mentions (into the "
                        "caption) that ping those users. Ignored on Telegram/ntfy."
                    ),
                },
                **_ntfy_options.SCHEMA_PROPERTIES,
            },
            "required": ["path"],
        },
    }


async def handle(state: AppState, *, path: str, channel: str | None = None,
                 caption: str | None = None, silent: bool = False,
                 supports_streaming: bool = True,
                 thread_id: str | None = None,
                 mention: list[str] | None = None,
                 priority: int | None = None,
                 title: str | None = None,
                 tags: list[str] | None = None) -> dict[str, Any]:
    _ = supports_streaming  # honored implicitly by tg_client.send_video defaults
    limits = state.settings.limits
    try:
        dest = state.settings.resolve_destination(channel)
    except ConfigError as e:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "unknown_channel", "channel": channel})
        return {"error": "invalid_input", "reason": "unknown_channel", "detail": str(e)}

    mention_ids, mention_note, mention_err = resolve_for_dest(dest, mention)
    if mention_err is not None:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "unknown_mention", "channel": dest.name,
                           "unknown": mention_err["unknown"]})
        return mention_err
    ntfy_kwargs, ntfy_note, ntfy_err = _ntfy_options.resolve_ntfy_options(
        dest, priority=priority, title=title, tags=tags,
    )
    if ntfy_err is not None:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": ntfy_err["reason"], "channel": dest.name})
        return ntfy_err

    resolved, err = _validate_path(
        path,
        allow_outside_cwd=state.settings.allow_outside_cwd,
        max_bytes=file_cap(limits, dest.transport),
    )
    if err is not None:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected", **err})
        return err
    assert resolved is not None

    if resolved.suffix.lower() not in _VIDEO_EXTS:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "not_a_video", "ext": resolved.suffix})
        return {"error": "invalid_input", "reason": "not_a_video",
                "ext": resolved.suffix, "allowed": sorted(_VIDEO_EXTS)}

    if mention_ids:
        caption = format_mention_prefix(mention_ids) + (caption or "")
    if caption:
        caption = truncate_caption(caption, limits.caption_max_chars)

    if not state.rate.try_acquire(TOOL_NAME):
        state.audit.write({"tool": TOOL_NAME, "status": "rejected", "reason": "rate_limit"})
        raise RateLimitError(TOOL_NAME)

    size = os.path.getsize(resolved)
    t0 = time.monotonic()
    try:
        sent = await send_video_file(
            state, dest, resolved, caption=caption, silent=silent, thread_id=thread_id,
            mention_user_ids=mention_ids or None, **ntfy_kwargs,
        )
    except TransportError as e:
        state.audit.write({
            "tool": TOOL_NAME, "status": "error",
            "transport": dest.transport, "channel": dest.name,
            "bytes": size, "path": str(resolved),
            "reason": "transport_unreachable", "error": e.detail,
        })
        return {"error": "transport_unreachable", "transport": dest.transport,
                "reason": e.detail,
                "hint": "video was NOT delivered — check network connectivity"}
    except (TelegramError, NtfyError, DiscordError) as e:
        state.audit.write({
            "tool": TOOL_NAME, "status": "error",
            "transport": dest.transport, "channel": dest.name,
            "bytes": size, "path": str(resolved),
            "error": getattr(e, "description", str(e)),
        })
        return {"error": f"{dest.transport}_api",
                "reason": getattr(e, "description", str(e))}
    except ConfigError as e:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "transport_unavailable", "channel": dest.name})
        return {"error": "transport_unavailable", "reason": str(e)}
    ms = int((time.monotonic() - t0) * 1000)
    record: dict[str, Any] = {
        "tool": TOOL_NAME, "status": "ok",
        "transport": dest.transport, "channel": dest.name,
        "chat_id": dest.chat_id, "ntfy_topic": dest.ntfy_topic,
        "bytes": size, "path": str(resolved), "ms": ms,
        "message_id": sent["message_id"],
    }
    if dest.transport == "discord":
        record["discord_webhook_id"] = webhook_id_from_url(dest.discord_webhook_url or "")
        record["thread_id"] = thread_id
        if mention_ids:
            record["mentions"] = list(mention or [])
    state.audit.write(record)
    result: dict[str, Any] = {"ok": True, "message_id": sent["message_id"], "bytes": size,
                              "channel": dest.name, "transport": dest.transport}
    notes = [n for n in (mention_note, ntfy_note) if n]
    if thread_id is not None and dest.transport != "discord":
        notes.append(f"thread_id ignored for {dest.transport}")
    if notes:
        result["note"] = "; ".join(notes)
    return result

