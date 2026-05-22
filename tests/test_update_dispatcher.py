"""UpdateDispatcher + from_operator tests."""

from __future__ import annotations

import pytest

from mcgram.update_dispatcher import UpdateDispatcher, from_operator


async def test_dispatch_calls_each_handler() -> None:
    seen: list[str] = []

    async def h1(upd: dict) -> None:
        seen.append("h1")

    async def h2(upd: dict) -> None:
        seen.append("h2")

    d = UpdateDispatcher()
    d.register(h1)
    d.register(h2)
    await d.dispatch({"update_id": 1})
    assert seen == ["h1", "h2"]


async def test_dispatch_swallows_handler_exceptions(caplog: pytest.LogCaptureFixture) -> None:
    async def bad(upd: dict) -> None:
        raise RuntimeError("boom")

    async def good_called(upd: dict) -> None:
        upd["seen"] = True

    d = UpdateDispatcher()
    d.register(bad)
    d.register(good_called)
    upd: dict = {}
    await d.dispatch(upd)
    assert upd.get("seen") is True


def test_from_operator_callback_query() -> None:
    upd = {"callback_query": {"id": "1", "message": {"chat": {"id": 42}}}}
    assert from_operator(upd, 42)
    assert not from_operator(upd, 999)


def test_from_operator_message() -> None:
    upd = {"message": {"chat": {"id": 7}, "text": "hi"}}
    assert from_operator(upd, 7)
    assert not from_operator(upd, 8)


def test_from_operator_unknown_update_type() -> None:
    assert not from_operator({"weird_event": {}}, 1)
