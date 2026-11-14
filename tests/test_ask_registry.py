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
        self, chat_id: int, message_id: int, reply_markup: Any = None, **kwargs: Any
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


async def test_tap_delivered_before_the_post_returns_still_answers() -> None:
    """getUpdates and sendMessage are separate requests: the operator's tap can
    reach the poll loop before sendMessage's own response does. Dropping that
    tap leaves the question hanging until it times out."""
    client = FakeClient()
    reg = AskRegistry()
    reg.set_answer_hook(client.answer_callback_query)
    post = client.send_message

    async def post_then_tap_lands_first(chat_id: int, text: str, **kw: Any) -> dict[str, Any]:
        msg = await post(chat_id, text, **kw)
        tap = kw["reply_markup"]["inline_keyboard"][1][0]["callback_data"]
        await reg.handle_update({"callback_query": {"id": "cq1", "data": tap}})
        return msg

    client.send_message = post_then_tap_lands_first  # type: ignore[method-assign]
    result = await asyncio.wait_for(
        reg.open(client, chat_id=1, question="Q?", options=["Yes", "No"], timeout_s=30), 2)
    assert (result["value"], result["source"]) == ("No", "button")


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


async def test_typed_message_is_never_an_answer() -> None:
    """Only a tap on the question's own buttons answers it; a typed message in
    the same chat must leave the question open until it times out."""
    client = FakeClient()
    reg = AskRegistry()

    async def type_a_reply() -> None:
        await _tap(reg)  # waits for the question to be open
        await reg.handle_update({"message": {"chat": {"id": 1}, "text": "yes please",
                                             "date": time.time() + 1}})

    drv = asyncio.create_task(type_a_reply())
    result = await reg.open(client, chat_id=1, question="Why?", options=["A"], timeout_s=1)
    await drv
    assert (result["value"], result["source"]) == ("", "timeout")


async def test_timeout() -> None:
    client = FakeClient()
    reg = AskRegistry()
    result = await reg.open(client, chat_id=1, question="Wait", options=["A", "B"], timeout_s=1)
    assert result["source"] == "timeout"
    assert result["value"] == ""
    # The question says it expired and loses its buttons, so nobody taps a dead ask.
    assert {"kind": "text", "message_id": 1001, "text": "Wait\n\n⏱ timed out"} in client.edited
    assert any(e["kind"] == "markup" and e["reply_markup"] is None for e in client.edited)


def test_build_keyboard_lays_out_columns_per_row() -> None:
    kb = _build_keyboard("q_x", ["A", "B", "C"], columns=2)
    assert kb["inline_keyboard"] == [
        [{"text": "A", "callback_data": "q_x:0"}, {"text": "B", "callback_data": "q_x:1"}],
        [{"text": "C", "callback_data": "q_x:2"}],
    ]


async def _tap(reg: AskRegistry, *suffixes: str) -> None:
    for _ in range(100):
        if reg.open_count:
            break
        await asyncio.sleep(0.01)
    q_id = next(iter(reg._pending))
    for n, suffix in enumerate(suffixes):
        await reg.handle_update({"callback_query": {"id": f"cq{n}", "data": f"{q_id}:{suffix}"}})


async def test_multi_select_toggles_and_returns_labels_on_done() -> None:
    client = FakeClient()
    reg = AskRegistry()
    # Tap C, A, then B twice (on, off): the result follows option order, not tap order.
    drv = asyncio.create_task(_tap(reg, "2", "0", "1", "1", "done"))
    result = await reg.open(client, chat_id=1, question="Which?", options=["A", "B", "C"],
                            timeout_s=5, multi_select=True, columns=3)
    await drv
    assert (result["value"], result["source"]) == (["A", "C"], "button")
    sent_rows = client.sent[0]["reply_markup"]["inline_keyboard"]
    assert [b["text"] for b in sent_rows[0]] == ["A", "B", "C"]
    [done] = sent_rows[1]
    assert done["text"] == "Done" and done["callback_data"].endswith(":done")
    redraws = [e["reply_markup"] for e in client.edited
               if e["kind"] == "markup" and e["reply_markup"] is not None]
    assert [b["text"] for b in redraws[1]["inline_keyboard"][0]] == ["✅ A", "B", "✅ C"]
    assert {"kind": "text", "message_id": 1001,
            "text": "Which?\n\n✅ Đã chọn: A, C"} in client.edited


async def test_multi_select_value_is_always_a_list() -> None:
    reg = AskRegistry()
    timed_out = await reg.open(FakeClient(), chat_id=1, question="?", options=["A"],
                               timeout_s=1, multi_select=True)
    assert timed_out["value"] == []


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


async def test_button_answer_echoed_into_message() -> None:
    """After a button tap the question message is rewritten to record the choice."""
    client = FakeClient()
    reg = AskRegistry()
    reg.set_answer_hook(client.answer_callback_query)

    async def driver() -> None:
        for _ in range(50):
            if reg.open_count:
                break
            await asyncio.sleep(0.01)
        q_id = next(iter(reg._pending))
        await reg.handle_update({"callback_query": {"id": "cq1", "data": f"{q_id}:1"}})

    drv = asyncio.create_task(driver())
    result = await reg.open(
        client, chat_id=1, question="Deploy?", options=["Yes", "No"], timeout_s=5
    )
    await drv
    assert result["value"] == "No"
    text_edits = [e for e in client.edited if e["kind"] == "text"]
    assert any("Deploy?" in e["text"] and "Đã chọn: No" in e["text"] for e in text_edits)


# Suppress "Unused" import on bare envs
_ = pytest
