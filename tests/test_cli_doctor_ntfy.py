"""cli_doctor tests for ntfy and hybrid transports."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from mcgram.cli_doctor import doctor

NTFY_TOPIC = "mcgram-doctortopic"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
HEALTH_URL = "https://ntfy.sh/v1/health"
REPLY_TOPIC = "mcgram-doctor-reply"
TOKEN_ENV = "MCGRAM_TEST_DOCTOR_NTFY_TOKEN"
TOKEN = "tk_doctor_secret"


def _write_ntfy_only(
    path: Path, audit: Path, topic: str = NTFY_TOPIC, *, ask: bool = False,
    server: str = "https://ntfy.sh",
) -> None:
    ask_block = (
        f"  access_token_env: {TOKEN_ENV}\n"
        f"  ask:\n    enabled: true\n    reply_topic: {REPLY_TOPIC}\n"
    ) if ask else ""
    path.write_text(
        f"""
ntfy:
  server: {server}
  default_topic: {topic}
{ask_block}audit:
  path: {audit.as_posix()}
""",
        encoding="utf-8",
    )


def _mock_topic_reads(
    httpx_mock, server: str = "https://ntfy.sh", *,
    readable: str | None = None, token_read: int = 200,
) -> None:
    """Topic reads as ntfy answers them: an anonymous read is denied (403)
    unless the topic is `readable`; a read with the token gets `token_read`."""
    def handle(request: httpx.Request) -> httpx.Response:
        topic = request.url.path[1:-len("/json")]
        if "Authorization" in request.headers:
            return httpx.Response(token_read)
        return httpx.Response(200 if topic == readable else 403)

    httpx_mock.add_callback(handle, url=re.compile(rf"{re.escape(server)}/[^/]+/json\?.*"),
                            method="GET", is_reusable=True, is_optional=True)


def _write_hybrid(path: Path, audit: Path) -> None:
    path.write_text(
        f"""
bot:
  token_env: TEST_TOKEN
  operator_chat_id: 42
ntfy:
  default_topic: {NTFY_TOPIC}
audit:
  path: {audit.as_posix()}
""",
        encoding="utf-8",
    )


@pytest.mark.parametrize("ask", [False, True], ids=["ask-off", "ask-on"])
def test_ntfy_only_happy_path_sends_nothing_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], httpx_mock, ask: bool,
) -> None:
    cfg = tmp_path / "config.yaml"
    _write_ntfy_only(cfg, tmp_path / "audit.jsonl", ask=ask)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    httpx_mock.add_response(url=HEALTH_URL, method="GET", json={"healthy": True})
    if ask:
        _mock_topic_reads(httpx_mock)
    rc = doctor()
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out
    assert "ntfy health" in out
    assert "[SKIP] ntfy send test" in out
    assert "[SKIP] telegram" in out
    assert ("[OK]   ntfy ask reply topic protected" in out) is ask
    assert ("[OK]   ntfy ask token reads reply topic" in out) is ask
    assert ("[SKIP] ntfy ask reply topic accepts anonymous publish" in out) is ask
    # Read-only: the probes and the token check are reads, and nothing is posted.
    requests = httpx_mock.get_requests()
    assert [r.method for r in requests] == ["GET"] * (4 if ask else 1)
    authed = [r for r in requests if r.headers.get("Authorization") == f"Bearer {TOKEN}"]
    assert [r.url.path for r in authed] == ([f"/{REPLY_TOPIC}/json"] if ask else [])
    assert TOKEN not in out


@pytest.mark.parametrize(
    ("server", "token_set", "readable", "token_read", "fail_row"),
    [
        ("https://ntfy.sh", True, NTFY_TOPIC, 200,
         "[FAIL] ntfy ask question topic protected"),
        ("https://ntfy.sh", True, REPLY_TOPIC, 200, "[FAIL] ntfy ask reply topic protected"),
        ("https://ntfy.sh", False, None, 200, "[FAIL] ntfy ask token"),
        ("http://ntfy.example.com", True, None, 200, "[FAIL] ntfy ask https"),
        ("https://ntfy.sh", True, None, 403, "[FAIL] ntfy ask token reads reply topic"),
    ],
    ids=["question-topic-readable", "reply-topic-readable", "no-token", "http-server",
         "token-cannot-read-reply-topic"],
)
def test_ntfy_ask_misconfiguration_fails_doctor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], httpx_mock,
    server: str, token_set: bool, readable: str | None, token_read: int, fail_row: str,
) -> None:
    cfg = tmp_path / "config.yaml"
    _write_ntfy_only(cfg, tmp_path / "audit.jsonl", ask=True, server=server)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    if token_set:
        monkeypatch.setenv(TOKEN_ENV, TOKEN)
    else:
        monkeypatch.delenv(TOKEN_ENV, raising=False)
    httpx_mock.add_response(url=f"{server}/v1/health", method="GET", json={"healthy": True})
    _mock_topic_reads(httpx_mock, server, readable=readable, token_read=token_read)
    rc = doctor()
    out = capsys.readouterr().out
    assert rc == 1
    assert fail_row in out
    assert out.count("[FAIL]") == 1


def test_ntfy_ask_send_checks_the_reply_topic_takes_anonymous_posts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], httpx_mock,
) -> None:
    cfg = tmp_path / "config.yaml"
    _write_ntfy_only(cfg, tmp_path / "audit.jsonl", ask=True)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    httpx_mock.add_response(url=HEALTH_URL, method="GET", json={"healthy": True})
    _mock_topic_reads(httpx_mock)
    reply_url = f"https://ntfy.sh/{REPLY_TOPIC}"
    httpx_mock.add_response(url=reply_url, method="POST", status_code=403, text="forbidden")
    httpx_mock.add_response(url=NTFY_URL, method="POST", json={"id": "n1"})
    rc = doctor(send=True)
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] ntfy ask reply topic accepts anonymous publish" in out
    anon = httpx_mock.get_request(url=reply_url, method="POST")
    assert anon is not None
    assert "Authorization" not in anon.headers


def test_ntfy_send_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], httpx_mock,
) -> None:
    cfg = tmp_path / "config.yaml"
    _write_ntfy_only(cfg, tmp_path / "audit.jsonl")
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    httpx_mock.add_response(url=HEALTH_URL, method="GET", json={"healthy": True})
    httpx_mock.add_response(url=NTFY_URL, method="POST",
                            status_code=500, text="server down")
    rc = doctor(send=True)
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
    assert "ntfy send test" in out


def test_ntfy_health_fails_marks_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], httpx_mock,
) -> None:
    cfg = tmp_path / "config.yaml"
    _write_ntfy_only(cfg, tmp_path / "audit.jsonl")
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    httpx_mock.add_response(url=HEALTH_URL, method="GET", status_code=503)
    rc = doctor()
    out = capsys.readouterr().out
    assert rc == 1
    assert "ntfy health" in out
    assert "non-200" in out


def test_hybrid_both_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], httpx_mock,
) -> None:
    cfg = tmp_path / "config.yaml"
    _write_hybrid(cfg, tmp_path / "audit.jsonl")
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    monkeypatch.setenv("TEST_TOKEN", "fake:tok")
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake:tok/getMe",
        json={"ok": True, "result": {"id": 1, "username": "bot"}},
    )
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake:tok/sendMessage",
        json={"ok": True, "result": {"message_id": 7}},
    )
    httpx_mock.add_response(url=HEALTH_URL, method="GET", json={"healthy": True})
    httpx_mock.add_response(url=NTFY_URL, method="POST", json={"id": "n1"})
    rc = doctor(send=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "[OK]   telegram send test" in out
    assert "[OK]   ntfy send test" in out
    assert "PASS" in out
