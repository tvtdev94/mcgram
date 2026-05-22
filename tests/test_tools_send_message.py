"""send_message tool handler tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcgram.errors import RateLimitError
from mcgram.rate_limiter import RateLimiter
from mcgram.runtime import AppState
from mcgram.tools import send_message


def _audit_records(audit_path: str | Path) -> list[dict]:
    p = Path(audit_path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


async def test_happy_path(app_state: AppState, httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/sendMessage",
        json={"ok": True, "result": {"message_id": 99}},
    )
    out = await send_message.handle(app_state, text="hello")
    assert out == {
        "ok": True, "message_id": 99, "channel": "default", "transport": "telegram",
    }
    records = _audit_records(app_state.audit.path)
    assert records[0]["tool"] == "send_message"
    assert records[0]["status"] == "ok"
    assert records[0]["text_len"] == 5


async def test_text_required(app_state: AppState) -> None:
    out = await send_message.handle(app_state, text="")
    assert out["error"] == "invalid_input"
    assert out["reason"] == "text_required"


async def test_text_too_long(app_state: AppState) -> None:
    out = await send_message.handle(app_state, text="x" * 5000)
    assert out["error"] == "invalid_input"
    assert out["reason"] == "text_too_long"


async def test_rate_limit(app_state: AppState, httpx_mock) -> None:
    # Replace bucket with a 1-token RateLimiter
    app_state.rate = RateLimiter(rate_per_min=1)
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/sendMessage",
        json={"ok": True, "result": {"message_id": 1}},
    )
    out1 = await send_message.handle(app_state, text="a")
    assert out1["ok"] is True
    with pytest.raises(RateLimitError):
        await send_message.handle(app_state, text="b")
    rejected = [r for r in _audit_records(app_state.audit.path) if r["status"] == "rejected"]
    assert any(r["reason"] == "rate_limit" for r in rejected)


async def test_telegram_error(app_state: AppState, httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/sendMessage",
        json={"ok": False, "description": "Bad Request: chat not found"},
        status_code=400,
    )
    out = await send_message.handle(app_state, text="hi")
    assert out["error"] == "telegram_api"
    assert "chat not found" in out["reason"]


async def test_silent_passed(app_state: AppState, httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/sendMessage",
        json={"ok": True, "result": {"message_id": 1}},
    )
    await send_message.handle(app_state, text="hi", silent=True)
    body = httpx_mock.get_requests()[0].content.decode()
    assert "disable_notification=True" in body or "disable_notification=true" in body


def test_schema_shape() -> None:
    s = send_message.schema()
    assert s["name"] == "send_message"
    assert "text" in s["inputSchema"]["required"]
