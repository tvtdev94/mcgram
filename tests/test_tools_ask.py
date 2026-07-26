"""ask tool handler tests."""

from __future__ import annotations

import asyncio
from typing import Any

from mcgram.ask_registry import AskRegistry
from mcgram.runtime import AppState
from mcgram.tools import ask


async def test_question_required(app_state: AppState) -> None:
    app_state.ask_registry = AskRegistry()
    out = await ask.handle(app_state, question="")
    assert out["error"] == "invalid_input"
    assert out["reason"] == "question_required"


async def test_too_many_options(app_state: AppState) -> None:
    app_state.ask_registry = AskRegistry()
    app_state.settings.limits.ask_options_max = 3
    out = await ask.handle(app_state, question="?", options=["a", "b", "c", "d"])
    assert out["error"] == "invalid_input"
    assert out["reason"] == "too_many_options"


async def test_registry_not_initialized(app_state: AppState) -> None:
    out = await ask.handle(app_state, question="?")
    assert out["error"] == "internal"
    assert out["reason"] == "ask_registry_not_initialized"


async def test_options_invalid_type(app_state: AppState) -> None:
    app_state.ask_registry = AskRegistry()
    out = await ask.handle(app_state, question="?", options=[1, 2])  # type: ignore[list-item]
    assert out["error"] == "invalid_input"


async def test_timeout_capped(app_state: AppState, httpx_mock) -> None:
    app_state.ask_registry = AskRegistry()
    app_state.settings.limits.ask_timeout_max_s = 1  # force tiny cap
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/sendMessage",
        json={"ok": True, "result": {"message_id": 1}},
    )
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/editMessageText",
        json={"ok": True, "result": True},
    )
    # Caller asks for 100s but limit forces 1s → should timeout fast
    out = await ask.handle(app_state, question="?", options=None, timeout_s=100)
    assert out["source"] == "timeout"


async def test_happy_button(app_state: AppState, httpx_mock) -> None:
    app_state.ask_registry = AskRegistry()
    app_state.ask_registry.set_answer_hook(app_state.client.answer_callback_query)
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/sendMessage",
        json={"ok": True, "result": {"message_id": 7}},
    )
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/answerCallbackQuery",
        json={"ok": True, "result": True},
    )
    # ask now rewrites the question message to record the choice, then strips buttons.
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/editMessageText",
        json={"ok": True, "result": True},
    )
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/editMessageReplyMarkup",
        json={"ok": True, "result": True},
    )

    async def driver() -> None:
        for _ in range(100):
            if app_state.ask_registry.open_count:
                break
            await asyncio.sleep(0.01)
        q_id = next(iter(app_state.ask_registry._pending))
        await app_state.ask_registry.handle_update({
            "callback_query": {"id": "cq", "data": f"{q_id}:0"}
        })

    drv = asyncio.create_task(driver())
    out = await ask.handle(app_state, question="Q?", options=["Yes", "No"], timeout_s=5)
    await drv
    assert out["value"] == "Yes"
    assert out["source"] == "button"


# Silence "unused" import in some envs
_ = Any
