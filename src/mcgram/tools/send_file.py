"""Tool: send_file — upload a local file to the operator chat."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ..errors import ConfigError, RateLimitError, TelegramError
from ..runtime import AppState

TOOL_NAME = "send_file"


def schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Send a local file as an attachment to a configured Telegram channel. "
            "Use for logs, screenshots, generated artifacts. Path must be inside CWD "
            "unless config sets allow_outside_cwd: true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative path to file"},
                "channel": {
                    "type": "string",
                    "description": "Named channel from config (default: 'default')",
                },
                "caption": {"type": "string", "description": "Optional caption (≤1024 chars)"},
                "silent": {"type": "boolean", "default": False},
            },
            "required": ["path"],
        },
    }


def _validate_path(
    raw: str, *, allow_outside_cwd: bool, max_bytes: int
) -> tuple[Path | None, dict[str, Any] | None]:
    """Return (path, None) on success, (None, error_dict) on rejection."""
    if not raw or not isinstance(raw, str):
        return None, {"error": "invalid_input", "reason": "path_required"}
    p = Path(raw).expanduser().resolve()
    if not p.exists():
        return None, {"error": "invalid_input", "reason": "file_not_found", "path": str(p)}
    if not p.is_file():
        return None, {"error": "invalid_input", "reason": "not_a_file", "path": str(p)}
    if not allow_outside_cwd:
        try:
            p.relative_to(Path.cwd().resolve())
        except ValueError:
            return None, {"error": "rejected", "reason": "path_outside_cwd", "path": str(p)}
    size = os.path.getsize(p)
    if size > max_bytes:
        return None, {
            "error": "rejected", "reason": "file_too_large",
            "bytes": size, "max_bytes": max_bytes,
        }
    return p, None


async def handle(state: AppState, *, path: str, channel: str | None = None,
                 caption: str | None = None,
                 silent: bool = False) -> dict[str, Any]:
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

    if caption and len(caption) > limits.caption_max_chars:
        caption = caption[: limits.caption_max_chars - 1] + "…"

    if not state.rate.try_acquire(TOOL_NAME):
        state.audit.write({"tool": TOOL_NAME, "status": "rejected", "reason": "rate_limit"})
        raise RateLimitError(TOOL_NAME)

    size = os.path.getsize(resolved)
    t0 = time.monotonic()
    try:
        msg = await state.client.send_document(
            chat_id, resolved, caption=caption, disable_notification=silent
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
