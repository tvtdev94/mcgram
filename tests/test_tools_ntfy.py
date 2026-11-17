"""Tools branching tests — ntfy transport path."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from email.header import decode_header, make_header
from pathlib import Path
from typing import Any

import pytest

from mcgram import server
from mcgram.audit import AuditLog
from mcgram.config import Settings
from mcgram.ntfy_client import NtfyClient
from mcgram.rate_limiter import RateLimiter
from mcgram.runtime import AppState
from mcgram.tg_client import TelegramClient
from mcgram.tools import ask, edit_message, send_file, send_message, send_video
from mcgram.update_dispatcher import UpdateDispatcher
from tests import fake_ntfy
from tests.fake_ntfy import FakeNtfy

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
        state = AppState(
            settings=ntfy_settings,
            dispatcher=UpdateDispatcher(),
            rate=RateLimiter(ntfy_settings.defaults.rate_limit_per_min),
            audit=AuditLog(ntfy_settings.audit.path),
            ntfy_client=nc,
        )
        # Wired as at boot: with ntfy.ask off this stays None.
        state.ntfy_ask = server._build_ntfy_ask(state)  # noqa: SLF001
        yield state


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
    out = await ask.handle(ntfy_app_state, question="proceed?", options=["ok"])
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


# --------------------------------------------------------------------------- #
# priority / title / tags
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("tool", ["send_message", "send_file", "send_video"])
async def test_ntfy_options_reach_headers(
    tool: str, ntfy_app_state, httpx_mock, tmp_path: Path
) -> None:
    vid = tmp_path / "demo.mp4"
    vid.write_bytes(b"\x00mp4payload")
    calls = {
        "send_message": lambda **kw: send_message.handle(ntfy_app_state, text="hi", **kw),
        "send_file": lambda **kw: send_file.handle(ntfy_app_state, path=str(vid), **kw),
        "send_video": lambda **kw: send_video.handle(ntfy_app_state, path=str(vid), **kw),
    }
    httpx_mock.add_response(url=NTFY_URL, json=_ntfy_ok())
    out = await calls[tool](priority=4, title="CI", tags=["warning", " rocket "])
    assert out["ok"] is True
    assert "note" not in out
    h = httpx_mock.get_requests()[0].headers
    assert (h.get("Priority"), h.get("Title"), h.get("Tags")) == ("4", "CI", "warning,rocket")


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"priority": 0}, "invalid_priority"),
        ({"priority": 6}, "invalid_priority"),
        ({"priority": True}, "invalid_priority"),
        ({"priority": "5"}, "invalid_priority"),
        ({"title": 3}, "invalid_title"),
        ({"tags": "warning"}, "invalid_tags"),
        ({"tags": ["a,b"]}, "invalid_tags"),
        ({"tags": [""]}, "invalid_tags"),
    ],
)
async def test_invalid_ntfy_options_rejected(
    kwargs: dict, reason: str, ntfy_app_state, httpx_mock
) -> None:
    out = await send_message.handle(ntfy_app_state, text="hi", **kwargs)
    assert out["error"] == "invalid_input"
    assert out["reason"] == reason
    assert httpx_mock.get_requests() == []


async def test_long_text_on_ntfy_goes_as_attachment_with_one_line_caption(
    ntfy_app_state, httpx_mock
) -> None:
    text = "header line\n" + "z" * 5000
    httpx_mock.add_response(url=NTFY_URL, method="PUT", json=_ntfy_ok())
    out = await send_message.handle(ntfy_app_state, text=text, priority=5)
    assert out["delivered_as"] == "file"
    req = httpx_mock.get_requests()[0]
    assert req.headers.get("Filename") == "message.txt"
    assert req.headers.get("Priority") == "5"
    caption = str(make_header(decode_header(req.headers["Message"])))
    assert caption.startswith("header line zzz")
    assert req.content == text.encode("utf-8")


async def test_edit_message_on_ntfy_is_unsupported_without_network(
    ntfy_app_state, httpx_mock
) -> None:
    out = await edit_message.handle(ntfy_app_state, message_id="kGWnD1QOQTWv", text="new")
    assert out["error"] == "transport_unsupported"
    assert httpx_mock.get_requests() == []
    assert _audit(ntfy_app_state.audit.path)[-1]["reason"] == "transport_unsupported"


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


# --------------------------------------------------------------------------- #
# ask over ntfy (ntfy.ask.enabled)
# --------------------------------------------------------------------------- #

ASK_TOKEN_ENV = "MCGRAM_TEST_NTFY_ASK_TOKEN"


def _ask_settings(tmp_path: Path, *, server_url: str = fake_ntfy.SERVER,
                  bot: bool = False) -> Settings:
    cfg = tmp_path / "config.yaml"
    bot_block = "bot:\n  token_env: TEST_TOKEN\n  operator_chat_id: 12345\n" if bot else ""
    cfg.write_text(
        f"""
{bot_block}ntfy:
  server: {server_url}
  default_topic: {fake_ntfy.QUESTION_TOPIC}
  access_token_env: {ASK_TOKEN_ENV}
  ask:
    enabled: true
    reply_topic: {fake_ntfy.REPLY_TOPIC}
channels:
  phone:
    transport: ntfy
audit:
  path: {(tmp_path / "audit.jsonl").as_posix()}
""",
        encoding="utf-8",
    )
    return Settings.load(cfg)


@contextlib.asynccontextmanager
async def _ask_state(
    settings: Settings, *, token: str | None, **extra: Any
) -> AsyncIterator[AppState]:
    """An AppState wired the way `server._run` wires it, shut down on exit."""
    assert settings.ntfy is not None
    async with NtfyClient(settings.ntfy.server, access_token=token) as nc:
        state = AppState(
            settings=settings,
            dispatcher=UpdateDispatcher(),
            rate=RateLimiter(settings.defaults.rate_limit_per_min),
            audit=AuditLog(settings.audit.path),
            ntfy_client=nc,
            **extra,
        )
        state.ntfy_ask = server._build_ntfy_ask(state)  # noqa: SLF001
        try:
            yield state
        finally:
            if state.ntfy_ask is not None:
                await state.ntfy_ask.shutdown()


@pytest.mark.parametrize(
    ("kwargs", "server_url", "token", "error", "reason"),
    [
        ({"multi_select": True}, fake_ntfy.SERVER, fake_ntfy.TOKEN,
         "invalid_input", "multi_select_unsupported"),
        ({"options": ["a", "b", "c", "d"]}, fake_ntfy.SERVER, fake_ntfy.TOKEN,
         "invalid_input", "too_many_options"),
        ({"question": "é" * 2049}, fake_ntfy.SERVER, fake_ntfy.TOKEN,
         "invalid_input", "question_too_long"),
        ({}, fake_ntfy.SERVER, None, "ntfy_ask_requires_token", None),
        ({}, "http://ntfy.example.com", fake_ntfy.TOKEN, "ntfy_ask_requires_https", None),
    ],
    ids=["multi-select", "four-options", "question-over-4096-bytes", "no-token", "http"],
)
async def test_ntfy_ask_refuses_before_any_network_call(
    tmp_path: Path, httpx_mock, kwargs: dict, server_url: str, token: str | None,
    error: str, reason: str | None,
) -> None:
    async with _ask_state(_ask_settings(tmp_path, server_url=server_url),
                          token=token) as state:
        args: dict[str, Any] = {"question": "Deploy?", "options": ["Yes", "No"], **kwargs}
        out = await ask.handle(state, **args)
    assert out["error"] == error
    if reason is not None:
        assert out["reason"] == reason
    assert out["channel"] == "default"
    assert httpx_mock.get_requests() == []
    rec = _audit(state.audit.path)[-1]
    assert (rec["status"], rec["transport"]) == ("rejected", "ntfy")


async def test_ntfy_ask_runs_where_telegram_ask_would_be_refused(
    tmp_path: Path, httpx_mock
) -> None:
    # This session has a bot but does not own polling: a Telegram ask returns
    # polling_not_owned here, an ntfy ask must not.
    fake = FakeNtfy(httpx_mock)
    async with TelegramClient("fake-token") as tg, _ask_state(
        _ask_settings(tmp_path, bot=True), token=fake_ntfy.TOKEN,
        client=tg, poll_owner_pid=4242,
    ) as state:
        call = asyncio.create_task(ask.handle(
            state, question="Deploy?", options=["Prod", "Staging"],
            channel="phone", columns=2,
        ))
        _, question = await fake.next_question()
        fake.broadcast(question["actions"][1]["body"])
        out = await call
    assert out["value"] == "Staging"
    assert out["source"] == "button"
    assert out["question_id"].startswith("q_")
    assert out["channel"] == "phone"
    assert out["note"] == "columns ignored on ntfy"
    rec = _audit(state.audit.path)[-1]
    assert (rec["tool"], rec["status"], rec["transport"], rec["question_id"]) == (
        "ask", "ok", "ntfy", out["question_id"])
    assert fake_ntfy.TOKEN not in Path(state.audit.path).read_text(encoding="utf-8")
    assert fake_ntfy.TOKEN not in json.dumps(out)
    assert fake.unexpected == []


async def test_ntfy_ask_on_a_readable_topic_reports_it_and_posts_nothing(
    tmp_path: Path, httpx_mock
) -> None:
    fake = FakeNtfy(httpx_mock)
    fake.probe_status[fake_ntfy.QUESTION_TOPIC] = 200
    async with _ask_state(_ask_settings(tmp_path), token=fake_ntfy.TOKEN) as state:
        out = await ask.handle(state, question="Deploy?", options=["Yes", "No"])
    assert out["error"] == "ntfy_topic_not_protected"
    assert out["topic"] == fake_ntfy.QUESTION_TOPIC
    assert "NOT posted" in out["hint"]
    assert fake.published == []
