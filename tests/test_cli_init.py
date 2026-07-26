"""cli_init smoke tests using a temp HOME."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcgram import cli_init
from mcgram.cli_init import init_config


@pytest.fixture(autouse=True)
def _no_claude_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip auto-register by default — only the dedicated test exercises it."""
    monkeypatch.setattr(cli_init, "_claude_cli_available", lambda: False)


@pytest.fixture(autouse=True)
def _no_tg_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep init offline: skip the Telegram reachability probe unless a test
    overrides `_probe_telegram_reachable` itself."""
    monkeypatch.setenv("MCGRAM_INIT_NO_TG_PROBE", "1")


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


def test_register_calls_claude_mcp_add(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd: list[str], **kw: object) -> _Result:
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(cli_init, "_claude_cli_available", lambda: True)
    monkeypatch.setattr(cli_init, "_is_already_registered", lambda: False)
    monkeypatch.setattr(cli_init.subprocess, "run", fake_run)

    init_config()
    out = capsys.readouterr().out
    assert "registered" in out
    add_call = next(c for c in calls if "add" in c)
    assert "mcgram" in add_call
    assert any("MCGRAM_CONFIG=" in arg for arg in add_call)


def test_register_skipped_when_already_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(cli_init, "_claude_cli_available", lambda: True)
    monkeypatch.setattr(cli_init, "_is_already_registered", lambda: True)
    init_config()
    out = capsys.readouterr().out
    assert "already in" in out


def test_init_sets_disable_polling_false_when_telegram_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(cli_init, "_probe_telegram_reachable", lambda *a, **k: True)
    init_config()
    text = (Path.home() / ".mcgram" / "config.yaml").read_text(encoding="utf-8")
    assert "disable_polling: false" in text
    assert "{{TG_DISABLE_POLLING}}" not in text  # placeholder fully substituted


def test_init_sets_disable_polling_true_when_telegram_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(cli_init, "_probe_telegram_reachable", lambda *a, **k: False)
    init_config()
    text = (Path.home() / ".mcgram" / "config.yaml").read_text(encoding="utf-8")
    assert "disable_polling: true" in text
    out = capsys.readouterr().out
    assert "unreachable" in out.lower()


def test_probe_reachable_true_on_any_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCGRAM_INIT_NO_TG_PROBE", raising=False)
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: object())
    assert cli_init._probe_telegram_reachable() is True


def test_probe_reachable_false_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCGRAM_INIT_NO_TG_PROBE", raising=False)
    import httpx

    def _boom(*_a: object, **_k: object) -> object:
        raise httpx.ConnectError("blocked")

    monkeypatch.setattr(httpx, "get", _boom)
    assert cli_init._probe_telegram_reachable() is False


def test_probe_skipped_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCGRAM_INIT_NO_TG_PROBE", "1")
    import httpx

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("probe must not touch the network when skipped")

    monkeypatch.setattr(httpx, "get", _boom)
    assert cli_init._probe_telegram_reachable() is True
