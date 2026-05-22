"""TelegramClient unit tests using pytest-httpx mock transport."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mcgram.errors import AuthError, TelegramError
from mcgram.tg_client import TelegramClient


async def test_get_me_success(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botT/getMe",
        json={"ok": True, "result": {"id": 1, "username": "bot"}},
    )
    async with TelegramClient("T") as c:
        result = await c.get_me()
        assert result["username"] == "bot"


async def test_send_message_passes_args(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botT/sendMessage",
        json={"ok": True, "result": {"message_id": 42}},
    )
    async with TelegramClient("T") as c:
        r = await c.send_message(7, "hi", parse_mode="MarkdownV2", disable_notification=True)
        assert r["message_id"] == 42
    request = httpx_mock.get_requests()[0]
    body = request.content.decode()
    assert "chat_id=7" in body
    assert "text=hi" in body
    assert "parse_mode=MarkdownV2" in body
    assert "disable_notification=True" in body or "disable_notification=true" in body


async def test_send_message_with_reply_markup(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botT/sendMessage",
        json={"ok": True, "result": {"message_id": 1}},
    )
    async with TelegramClient("T") as c:
        kb = {"inline_keyboard": [[{"text": "A", "callback_data": "x"}]]}
        await c.send_message(1, "q", reply_markup=kb)
    body = httpx_mock.get_requests()[0].content.decode()
    assert "reply_markup" in body


async def test_send_document(httpx_mock, tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    p.write_bytes(b"hello")
    httpx_mock.add_response(
        url="https://api.telegram.org/botT/sendDocument",
        json={"ok": True, "result": {"message_id": 5}},
    )
    async with TelegramClient("T") as c:
        r = await c.send_document(1, p, caption="cap")
        assert r["message_id"] == 5
    # multipart body should include the filename + caption
    body = httpx_mock.get_requests()[0].content
    assert b"f.txt" in body
    assert b"cap" in body


async def test_get_updates(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botT/getUpdates",
        json={"ok": True, "result": [{"update_id": 1}, {"update_id": 2}]},
    )
    async with TelegramClient("T") as c:
        updates = await c.get_updates(offset=0, timeout=1)
        assert [u["update_id"] for u in updates] == [1, 2]


async def test_answer_callback_query(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botT/answerCallbackQuery",
        json={"ok": True, "result": True},
    )
    async with TelegramClient("T") as c:
        assert await c.answer_callback_query("cq_id") is True


async def test_edit_message_text(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botT/editMessageText",
        json={"ok": True, "result": True},
    )
    async with TelegramClient("T") as c:
        await c.edit_message_text(1, 2, "new")


async def test_edit_message_reply_markup(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botT/editMessageReplyMarkup",
        json={"ok": True, "result": True},
    )
    async with TelegramClient("T") as c:
        await c.edit_message_reply_markup(1, 2, reply_markup={"inline_keyboard": []})


async def test_telegram_error_on_not_ok(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botT/sendMessage",
        json={"ok": False, "description": "chat not found"},
        status_code=400,
    )
    async with TelegramClient("T") as c:
        with pytest.raises(TelegramError) as exc:
            await c.send_message(1, "hi")
        assert "chat not found" in str(exc.value)


async def test_auth_error_on_401(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botT/getMe",
        json={"ok": False, "description": "Unauthorized"},
        status_code=401,
    )
    async with TelegramClient("T") as c:
        with pytest.raises(AuthError):
            await c.get_me()


async def test_empty_token_raises() -> None:
    with pytest.raises(AuthError):
        TelegramClient("")


async def test_call_outside_context_raises() -> None:
    c = TelegramClient("T")
    with pytest.raises(RuntimeError, match="outside"):
        await c.get_me()


async def test_non_json_response(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botT/getMe",
        content=b"not json",
        status_code=500,
    )
    async with TelegramClient("T") as c:
        with pytest.raises(TelegramError):
            await c.get_me()


async def test_custom_api_root(httpx_mock) -> None:
    httpx_mock.add_response(
        url="http://localhost:9999/botT/getMe",
        json={"ok": True, "result": {"id": 1}},
    )
    async with TelegramClient("T", api_root="http://localhost:9999") as c:
        await c.get_me()


# Suppress unused-import warning for `httpx`
_ = httpx
