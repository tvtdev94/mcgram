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


def test_default_routes_to_ntfy_when_telegram_blocked(tmp_path: Path) -> None:
    """bot present but polling disabled (blocked machine) + ntfy → default = ntfy."""
    s = _write(
        tmp_path / "c.yaml",
        """
bot:
  operator_chat_id: 1
  disable_polling: true
ntfy:
  default_topic: mcgram-abc
""",
    )
    dest = s.resolve_destination()
    assert dest.transport == "ntfy"
    assert dest.ntfy_topic == "mcgram-abc"


def test_default_routes_to_telegram_when_reachable(tmp_path: Path) -> None:
    """bot with polling on wins the default even when ntfy is also configured."""
    s = _write(
        tmp_path / "c.yaml",
        """
bot:
  operator_chat_id: 7
  disable_polling: false
ntfy:
  default_topic: mcgram-abc
""",
    )
    dest = s.resolve_destination()
    assert dest.transport == "telegram"
    assert dest.chat_id == 7


def test_default_telegram_sendonly_when_blocked_without_ntfy(tmp_path: Path) -> None:
    """No ntfy fallback: a blocked bot still seeds a (send-only) telegram default."""
    s = _write(
        tmp_path / "c.yaml",
        "bot:\n  operator_chat_id: 5\n  disable_polling: true\n",
    )
    dest = s.resolve_destination()
    assert dest.transport == "telegram"
    assert dest.chat_id == 5
