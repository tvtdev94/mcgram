"""AskRegistry unit tests with a fake client."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from mcgram.ask_registry import AskRegistry, _build_keyboard


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.answered: list[str] = []
        self._next_message_id = 1000

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> dict[str, Any]:
        self._next_message_id += 1
        msg = {"message_id": self._next_message_id, "text": text, "chat_id": chat_id, **kwargs}
        self.sent.append(msg)
        return msg

    async def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: Any = None
    ) -> None:
        self.edited.append({
            "kind": "markup", "message_id": message_id, "reply_markup": reply_markup,
        })

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, **kwargs: Any
    ) -> None:
        self.edited.append({"kind": "text", "message_id": message_id, "text": text})

    async def answer_callback_query(self, callback_query_id: str) -> bool:
        self.answered.append(callback_query_id)
        return True


def test_build_keyboard_one_button_per_row() -> None:
    kb = _build_keyboard("q_x", ["A", "B"])
    assert kb["inline_keyboard"] == [
        [{"text": "A", "callback_data": "q_x:0"}],
        [{"text": "B", "callback_data": "q_x:1"}],
    ]


async def test_button_resolves_ask() -> None:
    client = FakeClient()
    reg = AskRegistry()
    reg.set_answer_hook(client.answer_callback_query)

    async def driver() -> None:
        # Wait until the question is posted, then simulate a callback_query.
        for _ in range(50):
            if reg.open_count:
                break
            await asyncio.sleep(0.01)
        q_id = next(iter(reg._pending))
        await reg.handle_update({"callback_query": {"id": "cq1", "data": f"{q_id}:1"}})

    drv = asyncio.create_task(driver())
    result = await reg.open(client, chat_id=1, question="Q?", options=["Yes", "No"], timeout_s=5)
    await drv
    assert result["value"] == "No"
    assert result["source"] == "button"
    assert client.answered == ["cq1"]
    # markup stripped on resolve
    assert any(e["kind"] == "markup" and e["reply_markup"] is None for e in client.edited)


async def test_freetext_resolves_ask() -> None:
    client = FakeClient()
    reg = AskRegistry()

    async def driver() -> None:
        for _ in range(50):
            if reg.open_count:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)  # ensure msg.date > posted_at
        await reg.handle_update({
            "message": {
                "chat": {"id": 1},
                "text": "because reasons",
                "date": time.time() + 1,
            }
        })

    drv = asyncio.create_task(driver())
    result = await reg.open(client, chat_id=1, question="Why?", options=None, timeout_s=5)
    await drv
    assert result["value"] == "because reasons"
    assert result["source"] == "freetext"


async def test_timeout() -> None:
    client = FakeClient()
    reg = AskRegistry()
    result = await reg.open(client, chat_id=1, question="Wait", options=["A", "B"], timeout_s=1)
    assert result["source"] == "timeout"
    assert result["value"] == ""
    # message edited to show timeout text
    assert any(e["kind"] == "text" and "timed out" in e["text"] for e in client.edited)


async def test_concurrent_asks_resolve_independently() -> None:
    client = FakeClient()
    reg = AskRegistry()
    reg.set_answer_hook(client.answer_callback_query)

    async def resolve_first() -> None:
        for _ in range(100):
            if reg.open_count >= 2:
                break
            await asyncio.sleep(0.01)
        # Both asks open. Resolve oldest by callback button.
        ids = list(reg._pending)
        q1 = ids[0]
        await reg.handle_update({"callback_query": {"id": "x", "data": f"{q1}:0"}})

    async def open1() -> dict[str, Any]:
        return await reg.open(client, chat_id=1, question="Q1", options=["a"], timeout_s=5)

    async def open2() -> dict[str, Any]:
        return await reg.open(client, chat_id=2, question="Q2", options=["b"], timeout_s=5)

    driver = asyncio.create_task(resolve_first())
    r1, r2 = await asyncio.gather(open1(), open2(), return_exceptions=False)
    await driver
    # Resolver picked first registered ask (oldest); the other may resolve or time out.
    # At least one must be button-resolved.
    sources = {r1["source"], r2["source"]}
    assert "button" in sources


async def test_message_before_question_ignored() -> None:
    """A message posted BEFORE the ask should not resolve it."""
    client = FakeClient()
    reg = AskRegistry()

    # Open ask
    async def driver() -> None:
        for _ in range(50):
            if reg.open_count:
                break
            await asyncio.sleep(0.01)
        # Send a message with date BEFORE posted_at (simulated by 0)
        await reg.handle_update({"message": {"chat": {"id": 1}, "text": "old", "date": 0}})

    drv = asyncio.create_task(driver())
    # Use a short timeout so this resolves as timeout, NOT freetext
    result = await reg.open(client, chat_id=1, question="?", options=None, timeout_s=1)
    await drv
    assert result["source"] == "timeout"


async def test_callback_for_unknown_question_silently_answered() -> None:
    client = FakeClient()
    reg = AskRegistry()
    reg.set_answer_hook(client.answer_callback_query)
    await reg.handle_update({"callback_query": {"id": "cq", "data": "q_unknown:0"}})
    # No error; answer still sent
    assert client.answered == ["cq"]


async def test_callback_index_out_of_range() -> None:
    client = FakeClient()
    reg = AskRegistry()
    reg.set_answer_hook(client.answer_callback_query)

    async def driver() -> None:
        for _ in range(50):
            if reg.open_count:
                break
            await asyncio.sleep(0.01)
        q_id = next(iter(reg._pending))
        # Out-of-range index → ignored, but answer fires
        await reg.handle_update({"callback_query": {"id": "cq", "data": f"{q_id}:99"}})
        # Then valid resolve
        await reg.handle_update({"callback_query": {"id": "cq2", "data": f"{q_id}:0"}})

    drv = asyncio.create_task(driver())
    result = await reg.open(client, chat_id=1, question="?", options=["only"], timeout_s=5)
    await drv
    assert result["value"] == "only"


# Suppress "Unused" import on bare envs
_ = pytest
