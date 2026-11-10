"""Transport-aware dispatch helpers.

Tools call these to send to a resolved `Destination` without caring whether
the underlying transport is Telegram, ntfy.sh or Discord.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Destination
from .errors import ConfigError, TelegramError
from .video_meta import probe_video

if TYPE_CHECKING:
    from .runtime import AppState

# Images Telegram shows inline via sendPhoto, keyed by extension.
_PHOTO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_GIF_EXT = ".gif"
# sendPhoto: "The photo must be at most 10 MB in size."
_TG_PHOTO_MAX_BYTES = 10 * 1024 * 1024
# Telegram answers a photo it cannot take (bad dimensions, size) with 400/413;
# those fall back to a plain document. Other errors (auth, flood) do not.
_PHOTO_REJECT_STATUSES = {400, 413}


def is_image(path: Path) -> bool:
    """True for file types the chat apps preview inline (png/jpg/jpeg/webp/gif)."""
    suffix = path.suffix.lower()
    return suffix in _PHOTO_MIME or suffix == _GIF_EXT


def require_client(state: AppState, dest: Destination) -> None:
    """Raise ConfigError unless the client backing `dest.transport` is present.

    Public so callers that dispatch later (the reminder scheduler) can validate
    a destination up front instead of failing at delivery time.
    """
    if dest.transport == "telegram" and state.client is None:
        raise ConfigError(
            f"channel {dest.name!r} uses telegram transport but no Telegram client "
            "is configured (missing `bot` section in config)"
        )
    if dest.transport == "ntfy" and state.ntfy_client is None:
        raise ConfigError(
            f"channel {dest.name!r} uses ntfy transport but no ntfy client "
            "is configured (missing `ntfy` section in config)"
        )
    if dest.transport == "discord" and state.discord_client is None:
        raise ConfigError(
            f"channel {dest.name!r} uses discord transport but no Discord client "
            "is configured"
        )


async def send_text(
    state: AppState,
    dest: Destination,
    text: str,
    *,
    silent: bool = False,
    parse_mode: str | None = None,
    thread_id: str | None = None,
    mention_user_ids: list[str] | None = None,
    title: str | None = None,
    priority: int | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Dispatch a text message. Returns a dict with `message_id` (str/int).

    `thread_id` and `mention_user_ids` are honored only by discord (per-call
    runtime targets); `title`, `priority` and `tags` only by ntfy. The other
    transports ignore them.
    """
    require_client(state, dest)
    if dest.transport == "ntfy":
        assert state.ntfy_client is not None and dest.ntfy_topic is not None
        result = await state.ntfy_client.send_message(
            dest.ntfy_topic, text, silent=silent,
            title=title, priority=priority, tags=tags,
        )
        return {"message_id": result.get("id")}
    if dest.transport == "discord":
        assert state.discord_client is not None
        assert dest.discord_webhook_url is not None
        result = await state.discord_client.send_message(
            dest.discord_webhook_url, text,
            thread_id=thread_id,
            username=dest.discord_username,
            avatar_url=dest.discord_avatar_url,
            silent=silent,
            mention_user_ids=mention_user_ids,
        )
        return {"message_id": result.get("id")}
    # telegram
    assert state.client is not None and dest.chat_id is not None
    msg = await state.client.send_message(
        dest.chat_id, text,
        parse_mode=parse_mode,
        disable_notification=silent,
    )
    return {"message_id": msg.get("message_id")}


async def edit_text(
    state: AppState,
    dest: Destination,
    message_id: str,
    text: str,
    *,
    parse_mode: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Replace the text of a message sent earlier. Telegram and Discord only.

    `message_id` is the value a send returned, as a string; the caller has
    already checked it is numeric.
    """
    require_client(state, dest)
    if dest.transport == "ntfy":
        raise ConfigError("ntfy does not support editing a sent message")
    if dest.transport == "discord":
        assert state.discord_client is not None
        assert dest.discord_webhook_url is not None
        result = await state.discord_client.edit_message(
            dest.discord_webhook_url, message_id, text, thread_id=thread_id,
        )
        return {"message_id": result.get("id", message_id)}
    assert state.client is not None and dest.chat_id is not None
    await state.client.edit_message_text(
        dest.chat_id, int(message_id), text, parse_mode=parse_mode,
    )
    return {"message_id": int(message_id)}


async def send_document(
    state: AppState,
    dest: Destination,
    path: Path,
    *,
    caption: str | None = None,
    silent: bool = False,
    thread_id: str | None = None,
    mention_user_ids: list[str] | None = None,
    title: str | None = None,
    priority: int | None = None,
    tags: list[str] | None = None,
    inline_image: bool = False,
) -> dict[str, Any]:
    """Dispatch a file attachment. `thread_id`/`mention_user_ids` are discord-only
    and `title`/`priority`/`tags` ntfy-only (see send_text).

    With `inline_image`, an image goes to Telegram as a photo (GIF as an
    animation) so it shows in the chat, falling back to a document when
    Telegram refuses it. Discord and ntfy preview image attachments on their
    own. The result's `sent_as` says how it went out; `note` explains a fallback.
    """
    require_client(state, dest)
    shown_inline = inline_image and is_image(path)
    if dest.transport == "ntfy":
        assert state.ntfy_client is not None and dest.ntfy_topic is not None
        result = await state.ntfy_client.send_file(
            dest.ntfy_topic, path, caption=caption, silent=silent,
            title=title, priority=priority, tags=tags,
        )
        return {"message_id": result.get("id"), "sent_as": _sent_as(path)}
    if dest.transport == "discord":
        assert state.discord_client is not None
        assert dest.discord_webhook_url is not None
        result = await state.discord_client.send_file(
            dest.discord_webhook_url, path,
            content=caption,
            thread_id=thread_id,
            username=dest.discord_username,
            avatar_url=dest.discord_avatar_url,
            silent=silent,
            mention_user_ids=mention_user_ids,
        )
        return {"message_id": result.get("id"), "sent_as": _sent_as(path)}
    assert state.client is not None and dest.chat_id is not None
    out: dict[str, Any] = {}
    if (shown_inline and path.suffix.lower() != _GIF_EXT
            and os.path.getsize(path) > _TG_PHOTO_MAX_BYTES):
        out["note"] = "image is over Telegram's 10 MB photo limit; sent as document"
    elif shown_inline:
        try:
            return await _send_tg_image(state, dest, path, caption, silent)
        except TelegramError as e:
            if e.status not in _PHOTO_REJECT_STATUSES:
                raise
            out["note"] = f"telegram refused the image inline ({e.description}); sent as document"
    msg = await state.client.send_document(
        dest.chat_id, path, caption=caption, disable_notification=silent,
    )
    return {"message_id": msg.get("message_id"), "sent_as": "document", **out}


def _sent_as(path: Path) -> str:
    """Discord and ntfy render image attachments inline themselves."""
    return "photo" if is_image(path) else "document"


async def _send_tg_image(
    state: AppState, dest: Destination, path: Path, caption: str | None, silent: bool
) -> dict[str, Any]:
    assert state.client is not None and dest.chat_id is not None
    suffix = path.suffix.lower()
    if suffix == _GIF_EXT:
        msg = await state.client.send_animation(
            dest.chat_id, path, caption=caption, disable_notification=silent,
        )
        return {"message_id": msg.get("message_id"), "sent_as": "animation"}
    msg = await state.client.send_photo(
        dest.chat_id, path, mime=_PHOTO_MIME[suffix],
        caption=caption, disable_notification=silent,
    )
    return {"message_id": msg.get("message_id"), "sent_as": "photo"}


async def send_video_file(
    state: AppState,
    dest: Destination,
    path: Path,
    *,
    caption: str | None = None,
    silent: bool = False,
    thread_id: str | None = None,
    mention_user_ids: list[str] | None = None,
    title: str | None = None,
    priority: int | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Dispatch a video file (Telegram inline player or ntfy mobile inline).

    `thread_id`/`mention_user_ids` are discord-only and `title`/`priority`/`tags`
    ntfy-only (see send_text)."""
    require_client(state, dest)
    if dest.transport == "ntfy":
        assert state.ntfy_client is not None and dest.ntfy_topic is not None
        result = await state.ntfy_client.send_video(
            dest.ntfy_topic, path, caption=caption, silent=silent,
            title=title, priority=priority, tags=tags,
        )
        return {"message_id": result.get("id")}
    if dest.transport == "discord":
        assert state.discord_client is not None
        assert dest.discord_webhook_url is not None
        result = await state.discord_client.send_video(
            dest.discord_webhook_url, path,
            content=caption,
            thread_id=thread_id,
            username=dest.discord_username,
            avatar_url=dest.discord_avatar_url,
            silent=silent,
            mention_user_ids=mention_user_ids,
        )
        return {"message_id": result.get("id")}
    assert state.client is not None and dest.chat_id is not None
    msg = await state.client.send_video(
        dest.chat_id, path, caption=caption, disable_notification=silent,
        meta=await probe_video(path),
    )
    return {"message_id": msg.get("message_id")}
