"""server.main() error UX tests."""

from __future__ import annotations

import asyncio

import pytest

from mcgram import server as server_module
from mcgram.errors import AuthError, ConfigError, LockHeldError


def test_lock_held_clean_exit(monkeypatch: pytest.MonkeyPatch,
                              capsys: pytest.CaptureFixture[str]) -> None:
    async def boom() -> None:
        raise LockHeldError(12345, "/tmp/.lock")

    monkeypatch.setattr(server_module, "_run", boom)
    with pytest.raises(SystemExit) as exc:
        server_module.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "another instance is running" in err
    assert "12345" in err


def test_config_error_clean_exit(monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    async def boom() -> None:
        raise ConfigError("bad config")

    monkeypatch.setattr(server_module, "_run", boom)
    with pytest.raises(SystemExit) as exc:
        server_module.main()
    assert exc.value.code == 1
    assert "config error" in capsys.readouterr().err


def test_auth_error_clean_exit(monkeypatch: pytest.MonkeyPatch,
                                capsys: pytest.CaptureFixture[str]) -> None:
    async def boom() -> None:
        raise AuthError("bad token")

    monkeypatch.setattr(server_module, "_run", boom)
    with pytest.raises(SystemExit) as exc:
        server_module.main()
    assert exc.value.code == 1
    assert "auth error" in capsys.readouterr().err


def test_keyboard_interrupt_exit_130(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(server_module, "_run", boom)
    with pytest.raises(SystemExit) as exc:
        server_module.main()
    assert exc.value.code == 130


def test_env_bool() -> None:
    assert server_module._env_bool("___not_set___") is False


# unused-import silencer
_ = asyncio
