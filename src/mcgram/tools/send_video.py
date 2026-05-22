"""Tool: send_video — upload a video that plays in-chat (sendVideo endpoint)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ..errors import ConfigError, RateLimitError, TelegramError
from ..runtime import AppState
from .send_file import _validate_path

TOOL_NAME = "send_video"
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Upload a video to a Telegram channel using the sendVideo endpoint. "
            "Video plays in-chat with thumbnail and scrubber (unlike send_file which "
            "shows it as a downloadable attachment). Use for .mp4/.mov/.mkv/.webm/.m4v. "
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
                    "description": "Hint to Telegram that the file is streamable (mp4 H.264)",
                },
            },
            "required": ["path"],
        },
    }


async def handle(state: AppState, *, path: str, channel: str | None = None,
                 caption: str | None = None, silent: bool = False,
                 supports_streaming: bool = True) -> dict[str, Any]:
    limits = state.settings.limits
    try:
        chat_id = state.settings.resolve_channel(channel)
    except ConfigError as e:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "unknown_channel", "channel": channel})
        return {"error": "invalid_input", "reason": "unknown_channel", "detail": str(e)}

    resolved, err = _validate_path(
        path,
        allow_outside_cwd=state.settings.allow_outside_cwd,
        max_bytes=limits.file_max_bytes,
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
        msg = await state.client.send_video(
            chat_id, resolved, caption=caption,
            disable_notification=silent,
            supports_streaming=supports_streaming,
        )
    except TelegramError as e:
        state.audit.write({
            "tool": TOOL_NAME, "status": "error", "bytes": size,
            "path": str(resolved), "error": e.description,
        })
        return {"error": "telegram_api", "reason": e.description}
    ms = int((time.monotonic() - t0) * 1000)
    state.audit.write({
        "tool": TOOL_NAME, "status": "ok", "chat_id": chat_id,
        "channel": channel or "default",
        "bytes": size, "path": str(resolved), "ms": ms,
        "message_id": msg.get("message_id"),
    })
    return {"ok": True, "message_id": msg.get("message_id"), "bytes": size,
            "channel": channel or "default"}


_ = Path  # silence unused-import linter
