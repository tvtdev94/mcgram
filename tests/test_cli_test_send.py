"""`mcgram test-send` tests: real config file, real send path, mocked HTTP."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mcgram.cli import main as cli_main
from mcgram.cli_test_send import main

TG_SEND = "https://api.telegram.org/botTEST/sendMessage"
NTFY_URL = "https://ntfy.sh/mcgram-testsend"


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
bot:
  token_env: MCGRAM_TEST_SEND_TOKEN
  operator_chat_id: 42
ntfy:
  default_topic: mcgram-testsend
channels:
  alerts:
    transport: ntfy
    ntfy_topic: mcgram-testsend
audit:
  path: {(tmp_path / 'audit.jsonl').as_posix()}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    monkeypatch.setenv("MCGRAM_TEST_SEND_TOKEN", "TEST")
    return cfg


def test_sends_to_named_channel_and_audits(
    config: Path, httpx_mock, capsys: pytest.CaptureFixture[str]
) -> None:
    httpx_mock.add_response(url=TG_SEND, json={"ok": True, "result": {"message_id": 5}})
    assert main(["default", "--text", "hello there"]) == 0
    out = capsys.readouterr().out
    assert "[OK]   default  telegram  message_id=5" in out
    assert "RESULT: PASS" in out
    assert b"text=hello+there" in httpx_mock.get_requests()[0].content
    audit = [json.loads(x) for x in (config.parent / "audit.jsonl").read_text().splitlines()]
    assert audit[-1]["tool"] == "send_message" and audit[-1]["status"] == "ok"


def test_all_reports_each_channel_and_fails_on_any_error(
    config: Path, httpx_mock, capsys: pytest.CaptureFixture[str]
) -> None:
    httpx_mock.add_response(url=TG_SEND, json={"ok": True, "result": {"message_id": 1}})
    httpx_mock.add_response(url=NTFY_URL, status_code=500, text="boom")
    assert main(["--all"]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] alerts  ntfy_api: boom" in out
    assert "[OK]   default" in out
    assert "RESULT: FAIL (1)" in out


def test_unknown_channel_fails(config: Path, httpx_mock,
                               capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ghost"]) == 1
    assert "[FAIL] ghost  invalid_input: unknown_channel" in capsys.readouterr().out
    assert httpx_mock.get_requests() == []


def test_missing_config_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                              capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("MCGRAM_CONFIG", str(tmp_path / "missing.yaml"))
    assert main(["default"]) == 1
    assert "not found" in capsys.readouterr().out


def test_channel_or_all_required(config: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_wired_into_cli(config: Path, httpx_mock, monkeypatch: pytest.MonkeyPatch) -> None:
    httpx_mock.add_response(url=TG_SEND, json={"ok": True, "result": {"message_id": 1}})
    monkeypatch.setattr(sys, "argv", ["mcgram", "test-send", "default"])
    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 0
