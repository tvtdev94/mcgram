"""Tool: send_file — upload a local file to the operator chat (telegram or ntfy)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ..discord_client import webhook_id_from_url
from ..dispatch import send_document
from ..errors import ConfigError, DiscordError, NtfyError, RateLimitError, TelegramError
from ..runtime import AppState

TOOL_NAME = "send_file"


def schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Send a local file as an attachment to a configured channel. "
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
                 silent: bool = False,
                 thread_id: str | None = None) -> dict[str, Any]:
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

    if caption and len(caption) > limits.caption_max_chars:
        caption = caption[: limits.caption_max_chars - 1] + "…"

    if not state.rate.try_acquire(TOOL_NAME):
        state.audit.write({"tool": TOOL_NAME, "status": "rejected", "reason": "rate_limit"})
        raise RateLimitError(TOOL_NAME)

    size = os.path.getsize(resolved)
    t0 = time.monotonic()
    try:
        sent = await send_document(
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
