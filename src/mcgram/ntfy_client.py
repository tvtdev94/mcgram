"""ntfy.sh HTTP push client (httpx wrapper).

Sends with POST/PUT to `<server>/<topic>`. The two-way primitives (`publish`
with actions, `stream`, `poll`, `anonymous_read_denied`) exist only for the
opt-in ask over ntfy. Mirrors the TelegramClient shape so tools can branch by
transport.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
from collections.abc import AsyncGenerator, Iterable
from pathlib import Path
from typing import Any

import httpx

from .errors import AuthError, NtfyError, TransportError

_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# A subscription is idle between keepalives (45 s by default, kept under the
# Android app's 77 s timeout), so the read timeout must outlast one; past it the
# connection is dead.
_STREAM_TIMEOUT = httpx.Timeout(10.0, read=90.0)
# Sanity cap on one subscription line. Real ntfy messages are 4 KiB at most.
_MAX_LINE_CHARS = 64 * 1024

log = logging.getLogger("mcgram.ntfy")


class NtfyClient:
    """Thin async wrapper around the ntfy.sh HTTP API.

    Methods raise `NtfyError` on non-2xx responses; 401/403 → `AuthError`.
    """

    def __init__(
        self,
        server: str = "https://ntfy.sh",
        access_token: str | None = None,
    ) -> None:
        # TODO(ntfy-auth-e2e): Bearer token path is covered only by unit tests
        # using respx mocks. Add an integration test against a real self-hosted
        # ntfy server with auth enabled (e.g. `docker run binwiederhier/ntfy`
        # with `auth-default-access: deny-all` + a publisher user) before
        # claiming production support for paid/self-hosted ntfy.
        self._base = server.rstrip("/")
        self._token = access_token or None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> NtfyClient:
        self._client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _topic_url(self, topic: str) -> str:
        return f"{self._base}/{topic}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("NtfyClient used outside `async with` context")
        merged = {**self._auth_header(), **(headers or {})}
        try:
            resp = await self._client.request(method, url, content=content, headers=merged)
        except httpx.HTTPError as e:
            raise TransportError.from_httpx("ntfy", e, self._token) from e
        _raise_for_status(resp.status_code, resp.text)
        try:
            return dict(resp.json())
        except ValueError:
            return {"status": resp.status_code}

    @staticmethod
    def _meta_headers(
        silent: bool,
        *,
        title: str | None = None,
        priority: int | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, str]:
        """Title / Priority / Tags headers. An explicit `priority` wins over
        `silent` (which maps to priority 1, ntfy's `min`)."""
        headers: dict[str, str] = {}
        if title:
            headers["Title"] = _text_header(title)
        eff_priority = priority if priority is not None else (1 if silent else None)
        if eff_priority is not None:
            headers["Priority"] = str(eff_priority)
        if tags:
            headers["Tags"] = _text_header(",".join(tags))
        return headers

    async def send_message(
        self,
        topic: str,
        text: str,
        *,
        title: str | None = None,
        priority: int | None = None,
        tags: list[str] | None = None,
        markdown: bool = False,
        silent: bool = False,
    ) -> dict[str, Any]:
        """POST text to `<server>/<topic>`. Returns parsed JSON response."""
        headers: dict[str, str] = {
            "Content-Type": "text/plain; charset=utf-8",
            **self._meta_headers(silent, title=title, priority=priority, tags=tags),
        }
        if markdown:
            headers["Markdown"] = "yes"
        return await self._request(
            "POST", self._topic_url(topic),
            content=text.encode("utf-8"), headers=headers,
        )

    async def send_file(
        self,
        topic: str,
        path: Path,
        *,
        caption: str | None = None,
        silent: bool = False,
        title: str | None = None,
        priority: int | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """PUT binary body with Filename header. Returns parsed JSON response.

        The filename keeps its extension, which is what the ntfy apps use to
        preview image attachments inline."""
        return await self._upload(
            topic, path, caption=caption, silent=silent,
            title=title, priority=priority, tags=tags,
        )

    async def send_video(
        self,
        topic: str,
        path: Path,
        *,
        caption: str | None = None,
        silent: bool = False,
        title: str | None = None,
        priority: int | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Same wire shape as send_file; mobile app inline-plays video/* MIME."""
        return await self._upload(
            topic, path, caption=caption, silent=silent,
            title=title, priority=priority, tags=tags,
        )

    async def _upload(
        self,
        topic: str,
        path: Path,
        *,
        caption: str | None,
        silent: bool,
        title: str | None = None,
        priority: int | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {
            "Filename": _ascii_header(path.name),
            **self._meta_headers(silent, title=title, priority=priority, tags=tags),
        }
        mime, _ = mimetypes.guess_type(path.name)
        if mime:
            headers["Content-Type"] = mime
        if caption:
            headers["Message"] = _text_header(caption)
        with open(path, "rb") as f:
            body = f.read()
        return await self._request(
            "PUT", self._topic_url(topic),
            content=body, headers=headers,
        )

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    async def publish(
        self,
        topic: str,
        message: str,
        *,
        title: str | None = None,
        priority: int | None = None,
        actions: list[dict[str, Any]] | None = None,
        sequence_id: str | None = None,
    ) -> dict[str, Any]:
        """Publish as JSON, the only form that carries `actions` and `sequence_id`.

        ntfy takes a JSON publish at the server root, not the topic URL
        (https://docs.ntfy.sh/publish/ — "Publish as JSON").
        """
        fields: dict[str, Any] = {
            "title": title, "priority": priority,
            "actions": actions, "sequence_id": sequence_id,
        }
        payload: dict[str, Any] = {"topic": topic, "message": message}
        payload.update({k: v for k, v in fields.items() if v is not None})
        return await self._request(
            "POST", f"{self._base}/",
            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    async def stream(
        self, topic: str, *, since: str | None
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Subscribe to `topic` and yield each event (`open`, `message`, ...).

        Runs until the server closes the connection or an error ends it:
        401/403 raise `AuthError`, another status `NtfyError`, and a network
        failure or a silence past the read timeout a scrubbed `TransportError`.
        """
        if self._client is None:
            raise RuntimeError("NtfyClient used outside `async with` context")
        params = {"since": since} if since is not None else None
        try:
            async with self._client.stream(
                "GET", f"{self._topic_url(topic)}/json", params=params,
                headers=self._auth_header(), timeout=_STREAM_TIMEOUT,
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    _raise_for_status(resp.status_code, resp.text)
                async for line in resp.aiter_lines():
                    for event in _parse_events([line]):
                        yield event
        except httpx.HTTPError as e:
            raise TransportError.from_httpx("ntfy", e, self._token) from e

    async def poll(self, topic: str, *, since: str) -> list[dict[str, Any]]:
        """Return the cached events on `topic` after `since` (a message id or
        unix time), without holding a connection open."""
        if self._client is None:
            raise RuntimeError("NtfyClient used outside `async with` context")
        try:
            resp = await self._client.get(
                f"{self._topic_url(topic)}/json",
                params={"poll": "1", "since": since}, headers=self._auth_header(),
            )
        except httpx.HTTPError as e:
            raise TransportError.from_httpx("ntfy", e, self._token) from e
        _raise_for_status(resp.status_code, resp.text)
        return list(_parse_events(resp.text.splitlines()))

    async def anonymous_read_denied(self, topic: str) -> bool:
        """True when a read of `topic` WITHOUT credentials is refused (401/403).

        Never sends the token: with it, every topic would look protected.
        `since=none` asks for no messages, so a readable topic hands back
        nothing either. Any other status raises.
        """
        if self._client is None:
            raise RuntimeError("NtfyClient used outside `async with` context")
        try:
            resp = await self._client.get(
                f"{self._topic_url(topic)}/json", params={"poll": "1", "since": "none"},
            )
        except httpx.HTTPError as e:
            raise TransportError.from_httpx("ntfy", e, self._token) from e
        if resp.status_code in (401, 403):
            return True
        if 200 <= resp.status_code < 300:
            return False
        description = resp.text.strip() or f"HTTP {resp.status_code}"
        raise NtfyError(resp.status_code, description[:200])

    async def health(self) -> bool:
        """GET /v1/health. Return True if HTTP 200, False otherwise."""
        if self._client is None:
            raise RuntimeError("NtfyClient used outside `async with` context")
        try:
            resp = await self._client.get(f"{self._base}/v1/health")
        except httpx.HTTPError:
            return False
        return resp.status_code == 200


def _raise_for_status(status: int, text: str) -> None:
    """401/403 → `AuthError`; any other status >= 400 → `NtfyError`."""
    if status in (401, 403):
        raise AuthError(f"ntfy: unauthorized (HTTP {status})")
    if status >= 400:
        description = text.strip() or f"HTTP {status}"
        raise NtfyError(status, description[:200])


def _parse_events(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    """One JSON object per line; blank, oversized and non-object lines are dropped."""
    for line in lines:
        if not line.strip():
            continue
        if len(line) > _MAX_LINE_CHARS:
            log.debug("dropping an oversized ntfy event line (%d chars)", len(line))
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _flat(value: str) -> str:
    """Line breaks become spaces: a header value cannot carry CR/LF, and httpx
    refuses to send one that does (a multi-line caption would otherwise fail)."""
    return " ".join(value.splitlines())


def _text_header(value: str) -> str:
    """Header value for user text (Title, Tags, Message), UTF-8 preserved.

    httpx only sends ASCII-safe header values, so non-ASCII text goes out as
    one RFC 2047 base64 encoded word, which ntfy decodes in any header
    (https://docs.ntfy.sh/publish/ — "Message title" and "Tags & emojis").
    """
    flat = _flat(value)
    if flat.isascii():
        return flat
    return "=?UTF-8?B?" + base64.b64encode(flat.encode("utf-8")).decode("ascii") + "?="


def _ascii_header(value: str) -> str:
    """Single-line ASCII header value; non-ASCII collapses to '?'."""
    return _flat(value).encode("ascii", "replace").decode("ascii")
