"""cli_init smoke tests using a temp HOME."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcgram.cli_init import init_config


def test_init_creates_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                            capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    init_config()
    home = Path.home() / ".mcgram"
    assert (home / "config.yaml").exists()
    assert (home / ".env").exists()
    captured = capsys.readouterr()
    assert "created" in captured.out
    assert "Next steps" in captured.out


def test_init_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                         capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    init_config()
    cfg = Path.home() / ".mcgram" / "config.yaml"
    orig = cfg.read_text()
    init_config()  # second run
    assert cfg.read_text() == orig
    captured = capsys.readouterr()
    assert "skipped" in captured.out


def test_init_force_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    home = Path.home() / ".mcgram"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("# CUSTOM", encoding="utf-8")
    init_config(force=True)
    text = (home / "config.yaml").read_text()
    assert "# CUSTOM" not in text  # overwritten
    assert "bot:" in text  # has the bundled template
