"""CLI dispatcher tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mcgram.cli import main


def test_version(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from mcgram import __version__

    monkeypatch.setattr(sys, "argv", ["mcgram", "--version"])
    main()
    out = capsys.readouterr().out
    assert "mcgram" in out
    assert __version__ in out


def test_help(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["mcgram", "--help"])
    main()
    out = capsys.readouterr().out
    assert "USAGE" in out
    assert "init" in out
    assert "doctor" in out
    assert "audit" in out


def test_unknown_arg_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["mcgram", "garbage"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_init_subcommand(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["mcgram", "init"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0


def test_install_skill_subcommand(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr(sys, "argv", ["mcgram", "install-skill"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0


def test_audit_subcommand_no_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["mcgram", "audit", "--path", str(tmp_path / "x.jsonl")])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert "No audit entries" in capsys.readouterr().out
