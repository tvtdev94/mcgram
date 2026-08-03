"""Discord webhook client (httpx wrapper).

One-way only: POST to a webhook URL. No polling, no 2-way input. Mirrors the
NtfyClient shape so tools can branch by transport.

Unlike TelegramClient / NtfyClient, this client is constructed *without* a
credential: a Discord webhook URL is simultaneously the address and the secret,
and mcgram holds many of them (one per channel). The URL is therefore passed to
each method rather than stored on the instance.
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

import httpx

from .errors import DiscordError

_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# SUPPRESS_NOTIFICATIONS message flag — sends without pinging channel members.
_FLAG_SUPPRESS_NOTIFICATIONS = 4096

# Discord numeric error codes → human-readable reasons. These are finer-grained
# than the HTTP status and are what actually helps a user fix the problem.
_FRIENDLY_CODES: dict[int, str] = {
    10015: "webhook does not exist or was deleted — check the URL in ~/.mcgram/.env",
    10003: "thread_id is invalid, or the thread is not in this webhook's channel",
    220003: (
        "cannot create a thread in a text channel via webhook (forum channels only) — "
        "pass the thread_id of an existing thread instead"
    ),
    220001: "this is a forum channel — a thread_id of an existing post is required",
}


class DiscordClient:
    """Thin async wrapper around the Discord webhook HTTP API.

    Methods raise `DiscordError` on non-2xx responses, mapping the Discord error
    code to a human-readable reason where one is known.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> DiscordClient:
        self._client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _build_url(webhook_url: str, thread_id: str | None) -> httpx.URL:
        """Merge `wait=true` (and `thread_id` when given) into the webhook URL.

        `thread_id` is a *query string* parameter, never a body field — Discord
        silently ignores unknown body keys, so putting it in the body posts to
        the base channel instead of the thread. Merging via `httpx.URL` keeps any
        query the webhook URL may already carry intact.
        """
        params: dict[str, str] = {"wait": "true"}
        if thread_id is not None:
            params["thread_id"] = thread_id
        return httpx.URL(webhook_url).copy_merge_params(params)

    @staticmethod
    def _build_payload(
        content: str | None,
        username: str | None,
        avatar_url: str | None,
        silent: bool,
        mention_user_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build the JSON body, omitting keys whose value is None.

        Discord rejects explicit `null` for `username`/`avatar_url`, so absent
        keys must be dropped rather than sent as null.

        `allowed_mentions` is ALWAYS set: webhooks parse `@everyone`/`@here`/role
        mentions from raw content by default, so an empty `parse` list is the
        safe baseline that pings nobody. When `mention_user_ids` is provided, only
        those specific user IDs are whitelisted (`parse: []` + explicit `users`),
        which is why `<@id>` tokens the caller put in `content` actually ping.
        """
        payload: dict[str, Any] = {}
        if content is not None:
            payload["content"] = content
        if username is not None:
            payload["username"] = username
        if avatar_url is not None:
            payload["avatar_url"] = avatar_url
        if silent:
            payload["flags"] = _FLAG_SUPPRESS_NOTIFICATIONS
        if mention_user_ids:
            payload["allowed_mentions"] = {"parse": [], "users": list(mention_user_ids)}
        else:
            payload["allowed_mentions"] = {"parse": []}
        return payload

    def _friendly_reason(
        self, code: int | None, message: str, retry_after: float | None = None
    ) -> str:
        if code is not None and code in _FRIENDLY_CODES:
            return _FRIENDLY_CODES[code]
        if retry_after is not None:
            return (
                f"rate limit exceeded (~30 req/60s per webhook), "
                f"retry after {retry_after}s"
            )
        return message or "unknown error"

    def _handle(self, resp: httpx.Response) -> dict[str, Any]:
        """Turn a webhook response into a dict, or raise `DiscordError`."""
        if resp.status_code >= 400:
            code: int | None = None
            retry_after: float | None = None
            message = f"HTTP {resp.status_code}"
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                message = str(body.get("message", message))
                raw_code = body.get("code")
                code = raw_code if isinstance(raw_code, int) else None
                raw_retry = body.get("retry_after")
                retry_after = raw_retry if isinstance(raw_retry, (int, float)) else None
            reason = self._friendly_reason(code, message, retry_after)
            raise DiscordError(resp.status_code, reason[:200], code=code)
        # 2xx — body is present only when ?wait=true (else 204 No Content).
        try:
            return dict(resp.json())
        except ValueError:
            return {"status": resp.status_code}

    async def _request(
        self,
        method: str,
        url: httpx.URL,
        *,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("DiscordClient used outside `async with` context")
        resp = await self._client.request(
            method, url, json=json_body, data=data, files=files
        )
        return self._handle(resp)

    async def send_message(
        self,
        webhook_url: str,
        content: str,
        *,
        thread_id: str | None = None,
        username: str | None = None,
        avatar_url: str | None = None,
        silent: bool = False,
        mention_user_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST a text message. Returns the created message object (has `id`).

        `content` must already contain any `<@id>` mention tokens; this method
        only whitelists `mention_user_ids` in `allowed_mentions` so they ping.
        """
        url = self._build_url(webhook_url, thread_id)
        payload = self._build_payload(
            content, username, avatar_url, silent, mention_user_ids
        )
        return await self._request("POST", url, json_body=payload)

    async def send_file(
        self,
        webhook_url: str,
        path: Path,
        *,
        content: str | None = None,
        thread_id: str | None = None,
        username: str | None = None,
        avatar_url: str | None = None,
        silent: bool = False,
        mention_user_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Upload a file as `multipart/form-data` (payload_json + files[0])."""
        url = self._build_url(webhook_url, thread_id)
        payload = self._build_payload(
            content, username, avatar_url, silent, mention_user_ids
        )
        mime, _ = mimetypes.guess_type(path.name)
        with open(path, "rb") as f:
            body = f.read()
        files = {"files[0]": (path.name, body, mime or "application/octet-stream")}
        data = {"payload_json": json.dumps(payload)}
        return await self._request("POST", url, data=data, files=files)

    async def send_video(
        self,
        webhook_url: str,
        path: Path,
        *,
        content: str | None = None,
        thread_id: str | None = None,
        username: str | None = None,
        avatar_url: str | None = None,
        silent: bool = False,
        mention_user_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Same wire shape as `send_file` — Discord infers video from the file
        extension and needs no separate endpoint."""
        return await self.send_file(
            webhook_url,
            path,
            content=content,
            thread_id=thread_id,
            username=username,
            avatar_url=avatar_url,
            silent=silent,
            mention_user_ids=mention_user_ids,
        )

    async def health(self, webhook_url: str) -> dict[str, Any] | None:
        """GET the webhook URL. Return `{name, channel_id, guild_id}` on 200,
        or None on 401/404 (so callers decide how to present the failure)."""
        if self._client is None:
            raise RuntimeError("DiscordClient used outside `async with` context")
        try:
            resp = await self._client.get(webhook_url)
        except httpx.HTTPError:
            return None
        if resp.status_code == 200:
            try:
                return dict(resp.json())
            except ValueError:
                return None
        return None


def resolve_mentions(
    names: list[str], registry: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Map registered mention names to Discord user IDs.

    Returns `(ids, unknown)` where `ids` holds the resolved user IDs (in the
    order requested, duplicates preserved) and `unknown` holds any names absent
    from `registry`. Callers reject the whole request when `unknown` is non-empty.
    """
    ids: list[str] = []
    unknown: list[str] = []
    for name in names:
        uid = registry.get(name)
        if uid is None:
            unknown.append(name)
        else:
            ids.append(uid)
    return ids, unknown


def format_mention_prefix(user_ids: list[str]) -> str:
    """Render user IDs as a leading `<@id> ` run to prepend to message content.

    Returns "" for an empty list, so callers can unconditionally concatenate.
    """
    return "".join(f"<@{uid}> " for uid in user_ids)


def webhook_id_from_url(url: str) -> str | None:
    """Extract the webhook ID (the numeric segment) from a webhook URL, for audit.

    A webhook URL looks like `.../api/webhooks/<id>/<token>`; this returns `<id>`
    and NEVER the token. Returns None for a malformed URL — this is an audit-only
    path and must never raise into a call that is otherwise succeeding.
    """
    try:
        parts = httpx.URL(url).path.split("/")
    except Exception:
        return None
    if "webhooks" in parts:
        i = parts.index("webhooks")
        if i + 1 < len(parts) and parts[i + 1].isdigit():
            return parts[i + 1]
    return None
