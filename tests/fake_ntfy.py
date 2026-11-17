"""A scripted ntfy server behind pytest-httpx, for the two-way ask tests.

It routes requests the way ntfy does (anonymous probe, subscription stream,
catch-up poll, JSON publish) and records them. It does no answer matching: the
tests decide which lines each subscription receives and when.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

SERVER = "https://ntfy.example.com"
QUESTION_TOPIC = "mcgram-q-test"
REPLY_TOPIC = "mcgram-r-test"
TOKEN = "tk_fake_secret_value"
OPEN_TIME = 1700000000

# One planned subscription: "live" streams, an int is an HTTP status, an
# exception is raised on connect. Past the plan every connect fails.
Connection = str | int | Exception


class LiveStream(httpx.AsyncByteStream):
    """A subscription body whose lines arrive when the test pushes them."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | Exception | None] = asyncio.Queue()

    def push(self, event: dict[str, Any]) -> None:
        self._queue.put_nowait(json.dumps(event).encode("utf-8") + b"\n")

    def fail(self) -> None:
        """Drop the connection, as a sleeping laptop or a dead network does."""
        self._queue.put_nowait(httpx.ReadError("connection dropped"))

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def aclose(self) -> None:
        self._queue.put_nowait(None)


class FakeNtfy:
    def __init__(self, httpx_mock: Any) -> None:
        self.probe_status: dict[str, int] = {}
        self.connections: list[Connection] = ["live"]
        self.streams: list[LiveStream] = []
        self.stream_since: list[str | None] = []
        self.poll_since: list[str | None] = []
        self.on_poll: Callable[[], list[dict[str, Any]] | int] = list
        # (message id, JSON payload) for every publish, questions and markers.
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.unexpected: list[str] = []
        self._questions: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self._ids = itertools.count(1)
        httpx_mock.add_callback(self._handle, is_reusable=True, is_optional=True)

    # -- test controls ------------------------------------------------------ #

    def message(self, body: str, *, msg_id: str | None = None) -> dict[str, Any]:
        n = next(self._ids)
        return {"id": msg_id or f"m{n}", "time": OPEN_TIME + n, "event": "message",
                "topic": REPLY_TOPIC, "message": body}

    def broadcast(self, body: str) -> None:
        """Deliver one reply to every subscriber, as ntfy fans out."""
        event = self.message(body)
        for stream in self.streams:
            stream.push(event)

    async def next_question(self) -> tuple[str, dict[str, Any]]:
        return await asyncio.wait_for(self._questions.get(), timeout=5)

    async def wait_streams(self, n: int) -> None:
        async def until() -> None:
            while len(self.stream_since) < n:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(until(), timeout=10)

    # -- routing ------------------------------------------------------------ #

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        authed = request.headers.get("Authorization") == f"Bearer {TOKEN}"
        if request.method == "POST" and path == "/" and authed:
            return self._publish(json.loads(request.content))
        if request.method == "GET" and path.endswith("/json"):
            topic = path[1:-len("/json")]
            if params.get("poll") == "1" and "Authorization" not in request.headers:
                return httpx.Response(self.probe_status.get(topic, 403))
            if topic == REPLY_TOPIC and authed and params.get("poll") == "1":
                return self._poll(params.get("since"))
            if topic == REPLY_TOPIC and authed:
                return self._subscribe(params.get("since"))
        self.unexpected.append(f"{request.method} {request.url}")
        return httpx.Response(404)

    def _publish(self, payload: dict[str, Any]) -> httpx.Response:
        msg_id = f"pub{len(self.published) + 1}"
        self.published.append((msg_id, payload))
        if "actions" in payload:
            self._questions.put_nowait((msg_id, payload))
        return httpx.Response(200, json={"id": msg_id, "time": OPEN_TIME,
                                         "event": "message", "topic": payload["topic"]})

    def _poll(self, since: str | None) -> httpx.Response:
        self.poll_since.append(since)
        result = self.on_poll()
        if isinstance(result, int):
            return httpx.Response(result, text="poll failed")
        lines = "".join(json.dumps(ev) + "\n" for ev in result)
        return httpx.Response(200, text=lines)

    def _subscribe(self, since: str | None) -> httpx.Response:
        self.stream_since.append(since)
        plan = self.connections.pop(0) if self.connections else httpx.ConnectError("refused")
        if isinstance(plan, Exception):
            raise plan
        if isinstance(plan, int):
            return httpx.Response(plan, text="forbidden")
        stream = LiveStream()
        stream.push({"id": f"open{len(self.streams) + 1}", "time": OPEN_TIME,
                     "event": "open", "topic": REPLY_TOPIC})
        self.streams.append(stream)
        return httpx.Response(200, stream=stream)
