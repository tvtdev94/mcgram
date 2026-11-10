"""edit_message tool tests — Telegram path (Discord/ntfy live with their transport tests)."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from mcgram import server
from mcgram.runtime import AppState
from mcgram.tools import edit_message

EDIT_URL = "https://api.telegram.org/botfake-token/editMessageText"


def _audit(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


async def test_edits_text_of_sent_message(app_state: AppState, httpx_mock) -> None:
    httpx_mock.add_response(url=EDIT_URL, json={"ok": True, "result": {"message_id": 99}})
    out = await edit_message.handle(app_state, message_id=99, text="step 2/3 done")
    assert out == {"ok": True, "message_id": 99, "channel": "default", "transport": "telegram"}
    form = parse_qs(httpx_mock.get_requests()[0].content.decode())
    assert form == {"chat_id": ["12345"], "message_id": ["99"], "text": ["step 2/3 done"]}
    rec = _audit(app_state.audit.path)[-1]
    assert (rec["tool"], rec["status"], rec["message_id"]) == ("edit_message", "ok", "99")


async def test_accepts_string_id_and_markdown(app_state: AppState, httpx_mock) -> None:
    httpx_mock.add_response(url=EDIT_URL, json={"ok": True, "result": {"message_id": 7}})
    out = await edit_message.handle(app_state, message_id="7", text="*x*",
                                    parse_mode="markdown_v2")
    assert out["message_id"] == 7
    form = parse_qs(httpx_mock.get_requests()[0].content.decode())
    assert form["parse_mode"] == ["MarkdownV2"]


@pytest.mark.parametrize("bad", ["abc", "../1", "", True, None, 1.5])
async def test_rejects_non_numeric_message_id(app_state: AppState, httpx_mock, bad) -> None:
    out = await edit_message.handle(app_state, message_id=bad, text="x")
    assert out == {"error": "invalid_input", "reason": "invalid_message_id"}
    assert httpx_mock.get_requests() == []


async def test_text_over_cap_rejected(app_state: AppState, httpx_mock) -> None:
    out = await edit_message.handle(app_state, message_id=1, text="x" * 4097)
    assert out["reason"] == "text_too_long"
    assert out["max"] == 4096
    assert httpx_mock.get_requests() == []


async def test_telegram_refusal_is_reported(app_state: AppState, httpx_mock) -> None:
    httpx_mock.add_response(
        url=EDIT_URL, status_code=400,
        json={"ok": False, "description": "Bad Request: message is not modified"},
    )
    out = await edit_message.handle(app_state, message_id=1, text="same")
    assert out == {"error": "telegram_api", "reason": "Bad Request: message is not modified"}
    assert _audit(app_state.audit.path)[-1]["status"] == "error"


def test_registered_as_mcp_tool() -> None:
    names = [m.TOOL_NAME for m in server._phase2_tool_modules()]
    assert "edit_message" in names
