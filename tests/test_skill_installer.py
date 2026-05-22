"""Skill installer tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcgram.skill_installer import install_skill


def test_no_claude_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                       capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    rc = install_skill()
    assert rc == 0
    assert "~/.claude not found" in capsys.readouterr().out


def test_creates_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    rc = install_skill()
    assert rc == 0
    target = tmp_path / ".claude" / "skills" / "mcgram" / "SKILL.md"
    assert target.is_file()
    content = target.read_text(encoding="utf-8")
    assert "name: mcgram" in content


def test_idempotent_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                         capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    install_skill()
    capsys.readouterr()  # discard
    install_skill()
    assert "skipped" in capsys.readouterr().out


def test_force_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    install_skill()
    target = tmp_path / ".claude" / "skills" / "mcgram" / "SKILL.md"
    target.write_text("old content", encoding="utf-8")
    install_skill(force=True)
    assert "old content" not in target.read_text(encoding="utf-8")
