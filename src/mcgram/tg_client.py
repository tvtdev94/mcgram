"""Telegram Bot API async client (httpx wrapper)."""

from __future__ import annotations

import contextlib
import json
import logging
from asyncio import sleep as _sleep
from pathlib import Path
from typing import Any

import httpx

from .errors import AuthError, TelegramError, TransportError

_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# Longest flood-control wait honored with an in-call retry. A longer wait fails
# at once: blocking a tool call (and the Claude Code session) for minutes is
# worse than reporting the rate limit.
_MAX_FLOOD_WAIT_S = 30.0

log = logging.getLogger("mcgram.telegram")

# Form field -> (filename, path on disk, MIME type). Paths, not open handles,
# so a retried request can re-open and upload the whole file again.
UploadFiles = dict[str, tuple[str, Path, str]]


def _flood_wait_s(status: int, payload: dict[str, Any]) -> float | None:
    """Seconds Telegram asks us to wait on a 429, or None when not a flood wait."""
    if status != 429 and payload.get("error_code") != 429:
        return None
    params = payload.get("parameters")
    raw = params.get("retry_after") if isinstance(params, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
        return None
    return float(raw)


class TelegramClient:
    """Thin async wrapper around the Telegram Bot HTTP API.

    Owns one httpx.AsyncClient. Methods raise `TelegramError` on non-ok responses;
    401 is mapped to `AuthError` to make bad-token failures explicit at the call site.
    """

    def __init__(self, token: str, api_root: str = "https://api.telegram.org") -> None:
        if not token:
            raise AuthError("bot token is empty")
        self._token = token
        self._base = f"{api_root.rstrip('/')}/bot{token}"
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> TelegramClient:
        self._client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _call(
        self,
        method: str,
        *,
        data: dict[str, Any] | None = None,
        files: UploadFiles | None = None,
        read_timeout: float | None = None,
        retry_flood_wait: bool = True,
    ) -> Any:
        """POST one Bot API method and return its `result`.

        On a flood-control 429 the call sleeps the server's `retry_after` and
        retries once, unless the wait exceeds `_MAX_FLOOD_WAIT_S`.
        """
        if self._client is None:
            raise RuntimeError("TelegramClient used outside `async with` context")
        url = f"{self._base}/{method}"
        timeout: httpx.Timeout | httpx._config.UseClientDefault
        if read_timeout is not None:
            timeout = httpx.Timeout(read_timeout, connect=10.0)
        else:
            timeout = httpx.USE_CLIENT_DEFAULT  # type: ignore[assignment]
        retried = not retry_flood_wait
        while True:
            resp = await self._post(url, data, files, timeout)
            if resp.status_code == 401:
                raise AuthError("telegram: invalid bot token (HTTP 401)")
            try:
                payload = resp.json()
            except ValueError as e:
                raise TelegramError(resp.status_code, f"non-json body: {e}") from e
            if payload.get("ok"):
                return payload.get("result")
            wait = _flood_wait_s(resp.status_code, payload)
            if wait is not None and not retried and wait <= _MAX_FLOOD_WAIT_S:
                log.warning("telegram %s: flood wait %.0fs, retrying once", method, wait)
                retried = True
                await _sleep(wait)
                continue
            raise TelegramError(resp.status_code, str(payload.get("description", "unknown")))

    async def _post(
        self,
        url: str,
        data: dict[str, Any] | None,
        files: UploadFiles | None,
        timeout: httpx.Timeout | httpx._config.UseClientDefault,
    ) -> httpx.Response:
        assert self._client is not None
        try:
            with contextlib.ExitStack() as stack:
                opened = {
                    field: (name, stack.enter_context(open(path, "rb")), mime)
                    for field, (name, path, mime) in (files or {}).items()
                }
                return await self._client.post(
                    url, data=data, files=opened or None, timeout=timeout,
                )
        except httpx.HTTPError as e:
            raise TransportError.from_httpx("telegram", e, self._token) from e

    async def _send_upload(
        self,
        method: str,
        field: str,
        mime: str,
        chat_id: int,
        path: Path,
        *,
        caption: str | None,
        disable_notification: bool,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "disable_notification": disable_notification,
            **(extra or {}),
        }
        if caption:
            data["caption"] = caption
        return await self._call(method, data=data, files={field: (path.name, path, mime)})

    async def get_me(self) -> dict[str, Any]:
        return await self._call("getMe")

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_notification: bool = False,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup is not None:
            data["reply_markup"] = json.dumps(reply_markup)
        return await self._call("sendMessage", data=data)

    async def send_document(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: str | None = None,
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        return await self._send_upload(
            "sendDocument", "document", "application/octet-stream", chat_id, path,
            caption=caption, disable_notification=disable_notification,
        )

    async def send_photo(
        self,
        chat_id: int,
        path: Path,
        *,
        mime: str,
        caption: str | None = None,
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        """Send an image shown inline in the chat. Telegram caps photos at
        10 MB and rejects extreme dimensions (fall back to `send_document`)."""
        return await self._send_upload(
            "sendPhoto", "photo", mime, chat_id, path,
            caption=caption, disable_notification=disable_notification,
        )

    async def send_animation(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: str | None = None,
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        """Send a GIF that autoplays in the chat."""
        return await self._send_upload(
            "sendAnimation", "animation", "image/gif", chat_id, path,
            caption=caption, disable_notification=disable_notification,
        )

    async def send_video(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: str | None = None,
        disable_notification: bool = False,
        supports_streaming: bool = True,
        meta: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Send a video with in-chat playback (thumbnail + scrubber).

        `meta` holds `width`, `height` and `duration` (see `video_meta`); Telegram
        does not read them itself from uploads over about 10 MB.

        For non-video files use `send_document` instead — Telegram rejects
        unrecognized formats on `sendVideo`.
        """
        return await self._send_upload(
            "sendVideo", "video", "video/mp4", chat_id, path,
            caption=caption, disable_notification=disable_notification,
            extra={"supports_streaming": supports_streaming, **(meta or {})},
        )

    async def get_updates(
        self, *, offset: int = 0, timeout: int = 25
    ) -> list[dict[str, Any]]:
        data = {"offset": offset, "timeout": timeout}
        # Long-poll: server may hold for `timeout` seconds; read budget = timeout + slack.
        # No in-call flood-wait retry: `poll_loop` already backs off on any error,
        # and a hidden sleep here would stack on top of that backoff.
        result = await self._call(
            "getUpdates", data=data, read_timeout=timeout + 10, retry_flood_wait=False,
        )
        return result if isinstance(result, list) else []

    async def answer_callback_query(self, callback_query_id: str) -> bool:
        # Only ever called from the update dispatcher, inside the poll loop: a
        # flood-wait sleep here would stall getUpdates for every other reply.
        await self._call("answerCallbackQuery", data={"callback_query_id": callback_query_id},
                         retry_flood_wait=False)
        return True

    async def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: dict[str, Any] | None = None,
        *,
        retry_flood_wait: bool = True,
    ) -> Any:
        """`retry_flood_wait=False` for calls made from the poll loop (see `_call`)."""
        data: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        if reply_markup is not None:
            data["reply_markup"] = json.dumps(reply_markup)
        return await self._call("editMessageReplyMarkup", data=data,
                                retry_flood_wait=retry_flood_wait)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        retry_flood_wait: bool = True,
    ) -> Any:
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        return await self._call("editMessageText", data=data,
                                retry_flood_wait=retry_flood_wait)

    async def edit_message_caption(
        self, chat_id: int, message_id: int, caption: str, *, parse_mode: str | None = None
    ) -> Any:
        """Replace the caption of a photo, video, animation or document message."""
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": caption,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        return await self._call("editMessageCaption", data=data)
