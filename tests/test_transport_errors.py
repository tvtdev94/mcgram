"""Network failures must surface as a named error, never as a bare `internal`.

A notification tool that silently drops a message when the network is down is
worse than one that refuses loudly, so every transport wraps its httpx errors in
`TransportError` and each tool reports it with an audit line.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from mcgram.errors import TransportError
from mcgram.runtime import AppState
from mcgram.tools import send_message

TOKEN = "fake-token"
TG_SEND = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


def _audit(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


async def test_connect_failure_reports_transport_unreachable(
    app_state: AppState, httpx_mock
) -> None:
    httpx_mock.add_exception(httpx.ConnectError("All connection attempts failed"))
    out = await send_message.handle(app_state, text="build passed")
    assert out["error"] == "transport_unreachable"
    assert out["transport"] == "telegram"
    assert "ConnectError" in out["reason"]


async def test_connect_failure_is_audited(app_state: AppState, httpx_mock) -> None:
    """The one failure that must never be silent still leaves a trail."""
    httpx_mock.add_exception(httpx.ConnectError("All connection attempts failed"))
    await send_message.handle(app_state, text="build passed")
    records = [r for r in _audit(app_state.audit.path) if r["tool"] == "send_message"]
    assert len(records) == 1
    assert records[0]["status"] == "error"
    assert records[0]["reason"] == "transport_unreachable"


async def test_timeout_reports_transport_unreachable(
    app_state: AppState, httpx_mock
) -> None:
    httpx_mock.add_exception(httpx.ReadTimeout("timed out"))
    out = await send_message.handle(app_state, text="build passed")
    assert out["error"] == "transport_unreachable"
    assert "ReadTimeout" in out["reason"]


async def test_bot_token_never_leaks_into_error_or_audit(
    app_state: AppState, httpx_mock
) -> None:
    """httpx errors can embed the request URL — and Telegram's carries the token."""
    httpx_mock.add_exception(httpx.ConnectError(f"cannot connect to {TG_SEND}"))
    out = await send_message.handle(app_state, text="build passed")
    assert TOKEN not in json.dumps(out)
    assert "***" in out["reason"]
    assert TOKEN not in Path(app_state.audit.path).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("secret", "expected"),
    [
        ("s3cret", "ConnectError: boom at ***"),
        (None, "ConnectError: boom at s3cret"),
        ("", "ConnectError: boom at s3cret"),
    ],
)
def test_from_httpx_scrubs_only_real_secrets(secret: str | None, expected: str) -> None:
    err = TransportError.from_httpx(
        "telegram", httpx.ConnectError("boom at s3cret"), secret
    )
    assert err.detail == expected
    assert err.transport == "telegram"


def test_from_httpx_handles_empty_message() -> None:
    """httpx errors often stringify to nothing — don't emit a dangling colon."""
    err = TransportError.from_httpx("ntfy", httpx.ConnectError(""))
    assert err.detail == "ConnectError"
