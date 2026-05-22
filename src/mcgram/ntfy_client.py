"""ntfy.sh HTTP push client (httpx wrapper).

One-way only: POST/PUT to `<server>/<topic>`. No polling, no 2-way input.
Mirrors the TelegramClient shape so tools can branch by transport.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

import httpx

from .errors import AuthError, NtfyError

_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


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
        resp = await self._client.request(method, url, content=content, headers=merged)
        if resp.status_code in (401, 403):
            raise AuthError(f"ntfy: unauthorized (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            description = resp.text.strip() or f"HTTP {resp.status_code}"
            raise NtfyError(resp.status_code, description[:200])
        try:
            return dict(resp.json())
        except ValueError:
            return {"status": resp.status_code}

    @staticmethod
    def _silent_priority(silent: bool, priority: int | None) -> int | None:
        if priority is not None:
            return priority
        return 1 if silent else None

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
        headers: dict[str, str] = {"Content-Type": "text/plain; charset=utf-8"}
        if title:
            headers["Title"] = _ascii_header(title)
        eff_priority = self._silent_priority(silent, priority)
        if eff_priority is not None:
            headers["Priority"] = str(eff_priority)
        if tags:
            headers["Tags"] = ",".join(tags)
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
    ) -> dict[str, Any]:
        """PUT binary body with Filename header. Returns parsed JSON response."""
        return await self._upload(topic, path, caption=caption, silent=silent)

    async def send_video(
        self,
        topic: str,
        path: Path,
        *,
        caption: str | None = None,
        silent: bool = False,
    ) -> dict[str, Any]:
        """Same wire shape as send_file; mobile app inline-plays video/* MIME."""
        return await self._upload(topic, path, caption=caption, silent=silent)

    async def _upload(
        self,
        topic: str,
        path: Path,
        *,
        caption: str | None,
        silent: bool,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {"Filename": _ascii_header(path.name)}
        mime, _ = mimetypes.guess_type(path.name)
        if mime:
            headers["Content-Type"] = mime
        if caption:
            headers["Message"] = _ascii_header(caption)
        eff_priority = self._silent_priority(silent, None)
        if eff_priority is not None:
            headers["Priority"] = str(eff_priority)
        with open(path, "rb") as f:
            body = f.read()
        return await self._request(
            "PUT", self._topic_url(topic),
            content=body, headers=headers,
        )

    async def health(self) -> bool:
        """GET /v1/health. Return True if HTTP 200, False otherwise."""
        if self._client is None:
            raise RuntimeError("NtfyClient used outside `async with` context")
        try:
            resp = await self._client.get(f"{self._base}/v1/health")
        except httpx.HTTPError:
            return False
        return resp.status_code == 200


def _ascii_header(value: str) -> str:
    """ntfy HTTP headers must be latin-1 safe; collapse non-ASCII to '?'.

    Use RFC 2047 in callers if they need to send non-ASCII titles/captions.
    For now, this guard prevents httpx encoding errors on emoji etc.
    """
    return value.encode("ascii", "replace").decode("ascii")
