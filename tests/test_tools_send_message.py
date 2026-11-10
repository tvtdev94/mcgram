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


TG_DOC = "https://api.telegram.org/botfake-token/sendDocument"


def _multipart_field(body: bytes, name: str) -> bytes:
    """Value of one multipart form field (enough for these fixed fixtures)."""
    marker = f'name="{name}"'.encode()
    start = body.index(marker)
    value_start = body.index(b"\r\n\r\n", start) + 4
    return body[value_start: body.index(b"\r\n--", value_start)]


async def test_long_text_delivered_as_txt_attachment(
    app_state: AppState, httpx_mock, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    text = "Báo cáo\n" + "y" * 5000  # over Telegram's 4096 cap, non-ASCII kept
    httpx_mock.add_response(url=TG_DOC, json={"ok": True, "result": {"message_id": 77}})
    out = await send_message.handle(app_state, text=text)
    assert out["ok"] is True
    assert out["message_id"] == 77
    assert out["delivered_as"] == "file"
    assert out["bytes"] == len(text.encode("utf-8"))
    body = httpx_mock.get_requests()[0].content
    assert b'filename="message.txt"' in body
    assert _multipart_field(body, "document") == text.encode("utf-8")
    caption = _multipart_field(body, "caption").decode("utf-8")
    assert len(caption) == 1024
    assert caption.startswith("Báo cáo\nyyy") and caption.endswith("…")
    # The temporary copy is gone once the upload finished.
    assert list((tmp_path / "tmp").iterdir()) == []
    assert _audit_records(app_state.audit.path)[-1]["delivered_as"] == "file"


async def test_text_over_file_cap_rejected_without_network(
    app_state: AppState, httpx_mock
) -> None:
    app_state.settings.limits.file_max_bytes = 4500
    out = await send_message.handle(app_state, text="x" * 5000)
    assert out["error"] == "invalid_input"
    assert out["reason"] == "text_too_long"
    assert out["max_bytes"] == 4500
    assert httpx_mock.get_requests() == []


async def test_ntfy_options_ignored_on_telegram_with_note(
    app_state: AppState, httpx_mock
) -> None:
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/sendMessage",
        json={"ok": True, "result": {"message_id": 1}},
    )
    out = await send_message.handle(app_state, text="hi", priority=5, tags=["x"])
    assert out["ok"] is True
    assert out["note"] == "priority/title/tags ignored for telegram"
    body = httpx_mock.get_requests()[0].content.decode()
    assert "priority" not in body and "tags" not in body


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
