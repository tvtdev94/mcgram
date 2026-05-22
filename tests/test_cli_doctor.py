"""cli_doctor smoke tests using httpx_mock + temp config."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcgram.cli_doctor import doctor


def _write_config(path: Path, audit: Path) -> None:
    path.write_text(
        f"""
bot:
  token_env: TEST_TOKEN
  operator_chat_id: 42
audit:
  path: {audit.as_posix()}
""",
        encoding="utf-8",
    )


def test_no_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("MCGRAM_CONFIG", str(tmp_path / "missing.yaml"))
    rc = doctor()
    assert rc == 1
    assert "not found" in capsys.readouterr().out


def test_missing_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                       capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "config.yaml"
    _write_config(cfg, tmp_path / "audit.jsonl")
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    monkeypatch.delenv("TEST_TOKEN", raising=False)
    rc = doctor()
    assert rc == 1
    assert "FAIL" in capsys.readouterr().out


def test_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                    capsys: pytest.CaptureFixture[str], httpx_mock) -> None:
    cfg = tmp_path / "config.yaml"
    _write_config(cfg, tmp_path / "audit.jsonl")
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    monkeypatch.setenv("TEST_TOKEN", "fake:abcd1234")
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake:abcd1234/getMe",
        json={"ok": True, "result": {"id": 1, "username": "test_bot"}},
    )
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake:abcd1234/sendMessage",
        json={"ok": True, "result": {"message_id": 99}},
    )
    rc = doctor()
    assert rc == 0
    out = capsys.readouterr().out
    assert "test_bot" in out
    assert "PASS" in out


def test_get_me_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                      capsys: pytest.CaptureFixture[str], httpx_mock) -> None:
    cfg = tmp_path / "config.yaml"
    _write_config(cfg, tmp_path / "audit.jsonl")
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    monkeypatch.setenv("TEST_TOKEN", "bad")
    httpx_mock.add_response(
        url="https://api.telegram.org/botbad/getMe",
        json={"ok": False, "description": "Unauthorized"},
        status_code=401,
    )
    rc = doctor()
    assert rc == 1
    assert "FAIL" in capsys.readouterr().out
