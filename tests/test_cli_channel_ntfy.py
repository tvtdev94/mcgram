"""mcgram channel add-ntfy + list-with-transport CLI tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from mcgram.cli_channel import main


def _write_telegram_config(p: Path) -> None:
    p.write_text("bot:\n  operator_chat_id: 1\n", encoding="utf-8")


def _write_ntfy_config(p: Path) -> None:
    p.write_text(
        "ntfy:\n  default_topic: mcgram-default-x\n",
        encoding="utf-8",
    )


def test_add_ntfy_explicit_topic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = tmp_path / "c.yaml"
    _write_telegram_config(cfg)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    rc = main(["add-ntfy", "alerts", "--topic", "mcgram-myalerts", "-d", "Alerts"])
    assert rc == 0
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["channels"]["alerts"]["transport"] == "ntfy"
    assert data["channels"]["alerts"]["ntfy_topic"] == "mcgram-myalerts"
    assert data["channels"]["alerts"]["description"] == "Alerts"
    out = capsys.readouterr().out
    assert "subscribe" in out.lower()


def test_add_ntfy_random_topic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = tmp_path / "c.yaml"
    _write_telegram_config(cfg)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    rc = main(["add-ntfy", "alerts"])
    assert rc == 0
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    topic = data["channels"]["alerts"]["ntfy_topic"]
    assert re.fullmatch(r"mcgram-[0-9a-f]{16}", topic)


def test_add_ntfy_default_is_reserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = tmp_path / "c.yaml"
    _write_telegram_config(cfg)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    rc = main(["add-ntfy", "default", "--topic", "x"])
    assert rc == 2
    assert "reserved" in capsys.readouterr().err


def test_add_ntfy_existing_rejected_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = tmp_path / "c.yaml"
    _write_telegram_config(cfg)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    main(["add-ntfy", "alerts", "--topic", "first"])
    rc = main(["add-ntfy", "alerts", "--topic", "second"])
    assert rc == 1
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["channels"]["alerts"]["ntfy_topic"] == "first"


def test_add_ntfy_force_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "c.yaml"
    _write_telegram_config(cfg)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    main(["add-ntfy", "alerts", "--topic", "first"])
    main(["add-ntfy", "alerts", "--topic", "second", "--force"])
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["channels"]["alerts"]["ntfy_topic"] == "second"


def test_list_shows_transport_column_for_telegram(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = tmp_path / "c.yaml"
    _write_telegram_config(cfg)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    rc = main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "telegram" in out
    assert "chat_id=1" in out


def test_list_shows_transport_column_for_ntfy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = tmp_path / "c.yaml"
    _write_ntfy_config(cfg)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    rc = main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ntfy" in out
    assert "topic=mcgram-default-x" in out


def test_list_mixed_transports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = tmp_path / "c.yaml"
    _write_telegram_config(cfg)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    main(["add-ntfy", "alerts", "--topic", "mcgram-a"])
    main(["add", "team", "-100", "-d", "Team"])
    out = capsys.readouterr().out  # consume add outputs
    _ = out
    rc = main(["list"])
    assert rc == 0
    listing = capsys.readouterr().out
    assert "ntfy" in listing
    assert "telegram" in listing
    assert "mcgram-a" in listing
    assert "-100" in listing
