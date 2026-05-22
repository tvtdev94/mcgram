"""Channel resolution + default-channel auto-seed tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcgram.config import DEFAULT_CHANNEL, Settings
from mcgram.errors import ConfigError


def _write(path: Path, body: str) -> Settings:
    path.write_text(body, encoding="utf-8")
    return Settings.load(path)


def test_default_channel_auto_created(tmp_path: Path) -> None:
    s = _write(tmp_path / "c.yaml", "bot:\n  operator_chat_id: 42\n")
    assert s.channel_names() == [DEFAULT_CHANNEL]
    assert s.resolve_channel() == 42
    assert s.resolve_channel("default") == 42


def test_user_channels_keep_default(tmp_path: Path) -> None:
    s = _write(
        tmp_path / "c.yaml",
        """
bot:
  operator_chat_id: 1
channels:
  team:
    chat_id: -100200
  bugs:
    chat_id: -100201
    description: Bug reports
""",
    )
    assert set(s.channel_names()) == {DEFAULT_CHANNEL, "team", "bugs"}
    assert s.resolve_channel("team") == -100200
    assert s.resolve_channel("bugs") == -100201
    assert s.resolve_channel() == 1  # default still seeded


def test_explicit_default_override(tmp_path: Path) -> None:
    """User can explicitly redefine the default channel chat_id."""
    s = _write(
        tmp_path / "c.yaml",
        """
bot:
  operator_chat_id: 1
channels:
  default:
    chat_id: 999
""",
    )
    assert s.resolve_channel() == 999  # explicit wins, auto-seed skipped


def test_unknown_channel_raises(tmp_path: Path) -> None:
    s = _write(tmp_path / "c.yaml", "bot:\n  operator_chat_id: 1\n")
    with pytest.raises(ConfigError, match="unknown channel"):
        s.resolve_channel("nope")
