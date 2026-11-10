"""Tool: send_file — upload a local file to a configured channel."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ..config import LimitsConfig
from ..discord_client import format_mention_prefix, webhook_id_from_url
from ..dispatch import send_document
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

TOOL_NAME = "send_file"


def schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Send a local file as an attachment to a configured channel. "
            "Use for logs, screenshots, generated artifacts. Images "
            "(png/jpg/jpeg/webp/gif) show inline in the chat. Path must be inside "
            "CWD unless config sets allow_outside_cwd: true."
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
                "as_document": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Telegram: send an image as a plain file instead of an "
                        "inline photo. Discord/ntfy always preview images."
                    ),
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


def file_cap(limits: LimitsConfig, transport: str) -> int:
    """Largest upload in bytes for `transport` (global cap, tightened per transport)."""
    if transport == "ntfy":
        return min(limits.file_max_bytes, limits.ntfy_file_max_bytes)
    if transport == "discord":
        return min(limits.file_max_bytes, limits.discord_file_max_bytes)
    return limits.file_max_bytes


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
                 thread_id: str | None = None,
                 mention: list[str] | None = None,
                 as_document: bool = False,
                 priority: int | None = None,
                 title: str | None = None,
                 tags: list[str] | None = None) -> dict[str, Any]:
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

    # Prepend @mentions into the caption (Discord's `content`) before truncation,
    # so the mention run survives the caption cap.
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
        sent = await send_document(
            state, dest, resolved, caption=caption, silent=silent, thread_id=thread_id,
            mention_user_ids=mention_ids or None, inline_image=not as_document,
            **ntfy_kwargs,
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
                "hint": "file was NOT delivered — check network connectivity"}
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
        "message_id": sent["message_id"], "sent_as": sent["sent_as"],
    }
    if dest.transport == "discord":
        record["discord_webhook_id"] = webhook_id_from_url(dest.discord_webhook_url or "")
        record["thread_id"] = thread_id
        if mention_ids:
            record["mentions"] = list(mention or [])
    state.audit.write(record)
    result: dict[str, Any] = {"ok": True, "message_id": sent["message_id"], "bytes": size,
                              "channel": dest.name, "transport": dest.transport,
                              "sent_as": sent["sent_as"]}
    notes = [n for n in (mention_note, ntfy_note, sent.get("note")) if n]
    if thread_id is not None and dest.transport != "discord":
        notes.append(f"thread_id ignored for {dest.transport}")
    if notes:
        result["note"] = "; ".join(notes)
    return result
