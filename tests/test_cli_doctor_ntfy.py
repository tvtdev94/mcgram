"""cli_doctor tests for ntfy and hybrid transports."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcgram.cli_doctor import doctor

NTFY_TOPIC = "mcgram-doctortopic"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
HEALTH_URL = "https://ntfy.sh/v1/health"


def _write_ntfy_only(path: Path, audit: Path, topic: str = NTFY_TOPIC) -> None:
    path.write_text(
        f"""
ntfy:
  default_topic: {topic}
audit:
  path: {audit.as_posix()}
""",
        encoding="utf-8",
    )


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


def test_ntfy_only_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], httpx_mock,
) -> None:
    cfg = tmp_path / "config.yaml"
    _write_ntfy_only(cfg, tmp_path / "audit.jsonl")
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    httpx_mock.add_response(url=HEALTH_URL, method="GET", json={"healthy": True})
    httpx_mock.add_response(url=NTFY_URL, method="POST", json={"id": "n1"})
    rc = doctor()
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out
    assert "ntfy health" in out
    assert "ntfy send test" in out
    assert "[SKIP] telegram" in out


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
    rc = doctor()
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
    # send still mocked so doctor doesn't crash on missing response
    httpx_mock.add_response(url=NTFY_URL, method="POST", json={"id": "n1"})
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
    rc = doctor()
    out = capsys.readouterr().out
    assert rc == 0
    assert "telegram send test" in out
    assert "ntfy send test" in out
    assert "PASS" in out
