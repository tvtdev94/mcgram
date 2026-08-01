"""Tool: send_video — upload a video that plays in-chat."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ..discord_client import webhook_id_from_url
from ..dispatch import send_video_file
from ..errors import ConfigError, DiscordError, NtfyError, RateLimitError, TelegramError
from ..runtime import AppState
from .send_file import _validate_path

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
            },
            "required": ["path"],
        },
    }


async def handle(state: AppState, *, path: str, channel: str | None = None,
                 caption: str | None = None, silent: bool = False,
                 supports_streaming: bool = True,
                 thread_id: str | None = None) -> dict[str, Any]:
    _ = supports_streaming  # honored implicitly by tg_client.send_video defaults
    limits = state.settings.limits
    try:
        dest = state.settings.resolve_destination(channel)
    except ConfigError as e:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "unknown_channel", "channel": channel})
        return {"error": "invalid_input", "reason": "unknown_channel", "detail": str(e)}

    max_bytes = limits.file_max_bytes
    if dest.transport == "ntfy":
        max_bytes = min(max_bytes, limits.ntfy_file_max_bytes)
    elif dest.transport == "discord":
        max_bytes = min(max_bytes, limits.discord_file_max_bytes)
    resolved, err = _validate_path(
        path,
        allow_outside_cwd=state.settings.allow_outside_cwd,
        max_bytes=max_bytes,
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

    if caption and len(caption) > limits.caption_max_chars:
        caption = caption[: limits.caption_max_chars - 1] + "…"

    if not state.rate.try_acquire(TOOL_NAME):
        state.audit.write({"tool": TOOL_NAME, "status": "rejected", "reason": "rate_limit"})
        raise RateLimitError(TOOL_NAME)

    size = os.path.getsize(resolved)
    t0 = time.monotonic()
    try:
        sent = await send_video_file(
            state, dest, resolved, caption=caption, silent=silent, thread_id=thread_id,
        )
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
    state.audit.write(record)
    result: dict[str, Any] = {"ok": True, "message_id": sent["message_id"], "bytes": size,
                              "channel": dest.name, "transport": dest.transport}
    if thread_id is not None and dest.transport != "discord":
        result["note"] = f"thread_id ignored for {dest.transport}"
    return result


_ = Path  # silence unused-import linter
