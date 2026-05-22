"""Tools branching tests — ntfy transport path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mcgram.audit import AuditLog
from mcgram.config import Settings
from mcgram.ntfy_client import NtfyClient
from mcgram.rate_limiter import RateLimiter
from mcgram.runtime import AppState
from mcgram.tools import ask, send_file, send_message, send_video
from mcgram.update_dispatcher import UpdateDispatcher

NTFY_TOPIC = "mcgram-testxyz"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"


def _audit(p: str) -> list[dict]:
    pp = Path(p)
    if not pp.exists():
        return []
    return [
        json.loads(line)
        for line in pp.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _ntfy_ok(text: str = "ok") -> dict[str, Any]:
    return {"id": "ntfy_id_1", "topic": NTFY_TOPIC, "message": text}


@pytest.fixture
def ntfy_settings(tmp_path: Path) -> Settings:
    cfg = tmp_path / "config.yaml"
    audit_path = (tmp_path / "audit.jsonl").as_posix()
    cfg.write_text(
        f"""
ntfy:
  default_topic: {NTFY_TOPIC}
defaults:
  rate_limit_per_min: 20
audit:
  path: {audit_path}
allow_outside_cwd: true
""",
        encoding="utf-8",
    )
    return Settings.load(cfg)


@pytest.fixture
async def ntfy_app_state(ntfy_settings: Settings, tmp_path: Path) -> Any:
    async with NtfyClient() as nc:
        yield AppState(
            settings=ntfy_settings,
            dispatcher=UpdateDispatcher(),
            rate=RateLimiter(ntfy_settings.defaults.rate_limit_per_min),
            audit=AuditLog(ntfy_settings.audit.path),
            ntfy_client=nc,
        )


async def test_send_message_routes_to_ntfy(ntfy_app_state, httpx_mock) -> None:
    httpx_mock.add_response(url=NTFY_URL, method="POST", json=_ntfy_ok())
    out = await send_message.handle(ntfy_app_state, text="hello via ntfy")
    assert out["ok"] is True
    assert out["transport"] == "ntfy"
    assert out["channel"] == "default"
    assert out["message_id"] == "ntfy_id_1"


async def test_send_message_ntfy_audit_has_transport_field(
    ntfy_app_state, httpx_mock
) -> None:
    httpx_mock.add_response(url=NTFY_URL, method="POST", json=_ntfy_ok())
    await send_message.handle(ntfy_app_state, text="x")
    rec = _audit(ntfy_app_state.audit.path)[0]
    assert rec["transport"] == "ntfy"
    assert rec["ntfy_topic"] == NTFY_TOPIC
    assert rec["chat_id"] is None


async def test_send_message_ntfy_silent_maps_to_priority_one(
    ntfy_app_state, httpx_mock
) -> None:
    httpx_mock.add_response(url=NTFY_URL, method="POST", json=_ntfy_ok())
    await send_message.handle(ntfy_app_state, text="x", silent=True)
    req = httpx_mock.get_requests()[0]
    assert req.headers.get("Priority") == "1"


async def test_send_message_unknown_channel_does_not_hit_network(
    ntfy_app_state, httpx_mock
) -> None:
    out = await send_message.handle(ntfy_app_state, text="x", channel="ghost")
    assert out["error"] == "invalid_input"
    assert out["reason"] == "unknown_channel"
    assert httpx_mock.get_requests() == []


async def test_send_message_ntfy_api_error_audited(
    ntfy_app_state, httpx_mock
) -> None:
    httpx_mock.add_response(url=NTFY_URL, method="POST", status_code=429, text="rate-limited")
    out = await send_message.handle(ntfy_app_state, text="x")
    assert out["error"] == "ntfy_api"
    err_records = [r for r in _audit(ntfy_app_state.audit.path) if r["status"] == "error"]
    assert err_records
    assert err_records[0]["transport"] == "ntfy"


async def test_send_file_routes_to_ntfy(
    ntfy_app_state, httpx_mock, tmp_path: Path
) -> None:
    log_path = tmp_path / "build.log"
    log_path.write_bytes(b"log content here")
    httpx_mock.add_response(url=NTFY_URL, method="PUT", json=_ntfy_ok())
    out = await send_file.handle(
        ntfy_app_state, path=str(log_path), caption="see attached",
    )
    assert out["ok"] is True
    assert out["transport"] == "ntfy"
    req = httpx_mock.get_requests()[0]
    assert req.headers.get("Filename") == "build.log"
    assert req.headers.get("Message") == "see attached"


async def test_send_video_routes_to_ntfy(
    ntfy_app_state, httpx_mock, tmp_path: Path
) -> None:
    vid = tmp_path / "demo.mp4"
    vid.write_bytes(b"\x00mp4payload")
    httpx_mock.add_response(url=NTFY_URL, method="PUT", json=_ntfy_ok())
    out = await send_video.handle(ntfy_app_state, path=str(vid))
    assert out["ok"] is True
    assert out["transport"] == "ntfy"
    req = httpx_mock.get_requests()[0]
    assert req.headers.get("Content-Type") == "video/mp4"


async def test_ask_on_ntfy_returns_transport_unsupported(
    ntfy_app_state, httpx_mock
) -> None:
    out = await ask.handle(ntfy_app_state, question="proceed?")
    assert out["error"] == "transport_unsupported"
    assert out["channel"] == "default"
    # No network calls
    assert httpx_mock.get_requests() == []
    rec = _audit(ntfy_app_state.audit.path)[0]
    assert rec["status"] == "rejected"
    assert rec["reason"] == "transport_unsupported"


async def test_send_file_ntfy_rejects_file_above_ntfy_cap(
    ntfy_app_state, httpx_mock, tmp_path: Path, monkeypatch
) -> None:
    """Files exceeding ntfy_file_max_bytes are rejected before any network call,
    even when below the generic file_max_bytes cap."""
    monkeypatch.chdir(tmp_path)
    big = tmp_path / "big.bin"
    big.write_bytes(b"y" * 5000)
    # Telegram cap stays at 50 MB; ntfy cap lowered to 1000 bytes.
    ntfy_app_state.settings.limits.ntfy_file_max_bytes = 1000
    out = await send_file.handle(ntfy_app_state, path=str(big))
    assert out["error"] == "rejected"
    assert out["reason"] == "file_too_large"
    assert out["bytes"] == 5000
    assert out["max_bytes"] == 1000
    # No HTTP request issued
    assert httpx_mock.get_requests() == []


async def test_send_file_ntfy_uses_lower_of_two_caps(
    ntfy_app_state, httpx_mock, tmp_path: Path, monkeypatch
) -> None:
    """When ntfy cap > generic cap, effective max stays at generic cap."""
    monkeypatch.chdir(tmp_path)
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 500)
    ntfy_app_state.settings.limits.file_max_bytes = 100  # the tighter bound
    ntfy_app_state.settings.limits.ntfy_file_max_bytes = 10_000_000  # would allow
    out = await send_file.handle(ntfy_app_state, path=str(big))
    assert out["error"] == "rejected"
    assert out["reason"] == "file_too_large"
    assert out["max_bytes"] == 100


async def test_send_video_ntfy_respects_ntfy_cap(
    ntfy_app_state, httpx_mock, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    vid = tmp_path / "huge.mp4"
    vid.write_bytes(b"\x00" * 5000)
    ntfy_app_state.settings.limits.ntfy_file_max_bytes = 1000
    out = await send_video.handle(ntfy_app_state, path=str(vid))
    assert out["error"] == "rejected"
    assert out["reason"] == "file_too_large"
    assert httpx_mock.get_requests() == []


async def test_send_file_telegram_unaffected_by_ntfy_cap(
    app_state, httpx_mock, tmp_path: Path, monkeypatch
) -> None:
    """Telegram channels ignore ntfy_file_max_bytes — only file_max_bytes applies."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "a.txt"
    target.write_bytes(b"x" * 5000)
    # ntfy cap is tiny, but channel is telegram → should pass
    app_state.settings.limits.ntfy_file_max_bytes = 100
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/sendDocument",
        json={"ok": True, "result": {"message_id": 1}},
    )
    out = await send_file.handle(app_state, path=str(target))
    assert out["ok"] is True


async def test_send_message_no_client_returns_transport_unavailable(
    ntfy_settings: Settings, tmp_path: Path, httpx_mock
) -> None:
    # AppState without ntfy_client → dispatch should raise ConfigError caught as
    # transport_unavailable.
    state = AppState(
        settings=ntfy_settings,
        dispatcher=UpdateDispatcher(),
        rate=RateLimiter(ntfy_settings.defaults.rate_limit_per_min),
        audit=AuditLog(ntfy_settings.audit.path),
        ntfy_client=None,
    )
    out = await send_message.handle(state, text="x")
    assert out["error"] == "transport_unavailable"
    assert httpx_mock.get_requests() == []
