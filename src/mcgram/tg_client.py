"""Telegram Bot API async client (httpx wrapper)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .errors import AuthError, TelegramError

_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


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
        files: dict[str, Any] | None = None,
        read_timeout: float | None = None,
    ) -> Any:
        if self._client is None:
            raise RuntimeError("TelegramClient used outside `async with` context")
        url = f"{self._base}/{method}"
        timeout: httpx.Timeout | httpx._config.UseClientDefault
        if read_timeout is not None:
            timeout = httpx.Timeout(read_timeout, connect=10.0)
        else:
            timeout = httpx.USE_CLIENT_DEFAULT  # type: ignore[assignment]
        resp = await self._client.post(url, data=data, files=files, timeout=timeout)
        if resp.status_code == 401:
            raise AuthError("telegram: invalid bot token (HTTP 401)")
        try:
            payload = resp.json()
        except ValueError as e:
            raise TelegramError(resp.status_code, f"non-json body: {e}") from e
        if not payload.get("ok"):
            raise TelegramError(resp.status_code, str(payload.get("description", "unknown")))
        return payload.get("result")

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
        import json as _json
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup is not None:
            data["reply_markup"] = _json.dumps(reply_markup)
        return await self._call("sendMessage", data=data)

    async def send_document(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: str | None = None,
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "disable_notification": disable_notification,
        }
        if caption:
            data["caption"] = caption
        with open(path, "rb") as f:
            files = {"document": (path.name, f, "application/octet-stream")}
            return await self._call("sendDocument", data=data, files=files)

    async def send_video(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: str | None = None,
        disable_notification: bool = False,
        supports_streaming: bool = True,
    ) -> dict[str, Any]:
        """Send a video with in-chat playback (thumbnail + scrubber).

        For non-video files use `send_document` instead — Telegram rejects
        unrecognized formats on `sendVideo`.
        """
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "disable_notification": disable_notification,
            "supports_streaming": supports_streaming,
        }
        if caption:
            data["caption"] = caption
        with open(path, "rb") as f:
            files = {"video": (path.name, f, "video/mp4")}
            return await self._call("sendVideo", data=data, files=files)

    async def get_updates(
        self, *, offset: int = 0, timeout: int = 25
    ) -> list[dict[str, Any]]:
        data = {"offset": offset, "timeout": timeout}
        # Long-poll: server may hold for `timeout` seconds; read budget = timeout + slack.
        result = await self._call("getUpdates", data=data, read_timeout=timeout + 10)
        return result if isinstance(result, list) else []

    async def answer_callback_query(self, callback_query_id: str) -> bool:
        await self._call("answerCallbackQuery", data={"callback_query_id": callback_query_id})
        return True

    async def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        import json as _json
        data: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        if reply_markup is not None:
            data["reply_markup"] = _json.dumps(reply_markup)
        return await self._call("editMessageReplyMarkup", data=data)

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, *, parse_mode: str | None = None
    ) -> Any:
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        return await self._call("editMessageText", data=data)
