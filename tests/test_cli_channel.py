"""mcgram channel add/list/remove CLI tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mcgram.cli_channel import main


def _write_config(p: Path) -> None:
    p.write_text("bot:\n  operator_chat_id: 1\n", encoding="utf-8")


def test_list_shows_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                            capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "c.yaml"
    _write_config(cfg)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    rc = main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "default" in out
    assert "chat_id=1" in out


def test_add_persists_to_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                              capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "c.yaml"
    _write_config(cfg)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    rc = main(["add", "team", "-100123", "-d", "Team chat"])
    assert rc == 0
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["channels"]["team"]["chat_id"] == -100123
    assert data["channels"]["team"]["description"] == "Team chat"


def test_add_existing_rejected_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = tmp_path / "c.yaml"
    _write_config(cfg)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    main(["add", "team", "-100123"])
    rc = main(["add", "team", "-100999"])
    assert rc == 1
    # Original kept
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["channels"]["team"]["chat_id"] == -100123


def test_add_force_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "c.yaml"
    _write_config(cfg)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    main(["add", "team", "-100123"])
    main(["add", "team", "-100999", "--force"])
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["channels"]["team"]["chat_id"] == -100999


def test_add_default_is_reserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "c.yaml"
    _write_config(cfg)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    rc = main(["add", "default", "999"])
    assert rc == 2
    assert "reserved" in capsys.readouterr().err


def test_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "c.yaml"
    _write_config(cfg)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    main(["add", "team", "-100123"])
    rc = main(["remove", "team"])
    assert rc == 0
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "team" not in (data.get("channels") or {})


def test_remove_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                        capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "c.yaml"
    _write_config(cfg)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    rc = main(["remove", "nope"])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_remove_default_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "c.yaml"
    _write_config(cfg)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    rc = main(["remove", "default"])
    assert rc == 2
    assert "reserved" in capsys.readouterr().err


def test_missing_config_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCGRAM_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(SystemExit) as exc:
        main(["list"])
    assert exc.value.code == 2
