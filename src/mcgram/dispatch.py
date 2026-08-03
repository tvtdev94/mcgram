"""Transport-aware dispatch helpers.

Tools call these to send to a resolved `Destination` without caring whether
the underlying transport is Telegram or ntfy.sh.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Destination
from .errors import ConfigError

if TYPE_CHECKING:
    from .runtime import AppState


def _require_client(state: AppState, dest: Destination) -> None:
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
) -> dict[str, Any]:
    """Dispatch a text message. Returns a dict with `message_id` (str/int).

    `thread_id` and `mention_user_ids` are honored only by discord (per-call
    runtime targets); the other transports ignore them.
    """
    _require_client(state, dest)
    if dest.transport == "ntfy":
        assert state.ntfy_client is not None and dest.ntfy_topic is not None
        result = await state.ntfy_client.send_message(
            dest.ntfy_topic, text, silent=silent,
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


async def send_document(
    state: AppState,
    dest: Destination,
    path: Path,
    *,
    caption: str | None = None,
    silent: bool = False,
    thread_id: str | None = None,
    mention_user_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Dispatch a file attachment. `thread_id`/`mention_user_ids` are discord-only
    (see send_text)."""
    _require_client(state, dest)
    if dest.transport == "ntfy":
        assert state.ntfy_client is not None and dest.ntfy_topic is not None
        result = await state.ntfy_client.send_file(
            dest.ntfy_topic, path, caption=caption, silent=silent,
        )
        return {"message_id": result.get("id")}
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
        return {"message_id": result.get("id")}
    assert state.client is not None and dest.chat_id is not None
    msg = await state.client.send_document(
        dest.chat_id, path, caption=caption, disable_notification=silent,
    )
    return {"message_id": msg.get("message_id")}


async def send_video_file(
    state: AppState,
    dest: Destination,
    path: Path,
    *,
    caption: str | None = None,
    silent: bool = False,
    thread_id: str | None = None,
    mention_user_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Dispatch a video file (Telegram inline player or ntfy mobile inline).

    `thread_id`/`mention_user_ids` are discord-only (see send_text)."""
    _require_client(state, dest)
    if dest.transport == "ntfy":
        assert state.ntfy_client is not None and dest.ntfy_topic is not None
        result = await state.ntfy_client.send_video(
            dest.ntfy_topic, path, caption=caption, silent=silent,
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
    )
    return {"message_id": msg.get("message_id")}
