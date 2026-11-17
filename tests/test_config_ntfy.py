"""Settings tests for ntfy transport and dual-transport configs."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcgram.config import DEFAULT_CHANNEL, Destination, Settings
from mcgram.errors import ConfigError


def _write(p: Path, body: str) -> Settings:
    p.write_text(body, encoding="utf-8")
    return Settings.load(p)


def test_ntfy_only_config_loads_without_bot_section(tmp_path: Path) -> None:
    s = _write(
        tmp_path / "c.yaml",
        """
ntfy:
  default_topic: mcgram-abc123
""",
    )
    assert s.bot is None
    assert s.ntfy is not None
    assert s.ntfy.default_topic == "mcgram-abc123"
    assert s.ntfy.server == "https://ntfy.sh"


def test_telegram_only_config_still_works(tmp_path: Path) -> None:
    s = _write(tmp_path / "c.yaml", "bot:\n  operator_chat_id: 42\n")
    assert s.bot is not None and s.bot.operator_chat_id == 42
    assert s.ntfy is None
    # Default channel resolves as telegram with legacy method
    assert s.resolve_channel() == 42


def test_no_bot_no_ntfy_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="bot.*ntfy"):
        _write(tmp_path / "c.yaml", "defaults:\n  ask_timeout_s: 30\n")


def test_default_channel_seeds_from_ntfy_when_no_telegram(tmp_path: Path) -> None:
    s = _write(
        tmp_path / "c.yaml",
        """
ntfy:
  default_topic: mcgram-topicX
""",
    )
    assert s.channel_names() == [DEFAULT_CHANNEL]
    dest = s.resolve_destination()
    assert dest.transport == "ntfy"
    assert dest.ntfy_topic == "mcgram-topicX"
    assert dest.ntfy_server == "https://ntfy.sh"


def test_default_channel_seeds_from_bot_when_no_ntfy(tmp_path: Path) -> None:
    s = _write(tmp_path / "c.yaml", "bot:\n  operator_chat_id: 11\n")
    dest = s.resolve_destination()
    assert dest.transport == "telegram"
    assert dest.chat_id == 11


def test_hybrid_default_prefers_telegram(tmp_path: Path) -> None:
    """When both bot and ntfy configured (and no explicit `default` channel),
    seed default as telegram to preserve pre-ntfy behavior."""
    s = _write(
        tmp_path / "c.yaml",
        """
bot:
  operator_chat_id: 5
ntfy:
  default_topic: mcgram-h
""",
    )
    dest = s.resolve_destination()
    assert dest.transport == "telegram"
    assert dest.chat_id == 5


def test_explicit_default_ntfy_channel_overrides_telegram_seed(tmp_path: Path) -> None:
    s = _write(
        tmp_path / "c.yaml",
        """
bot:
  operator_chat_id: 5
ntfy:
  default_topic: mcgram-h
channels:
  default:
    transport: ntfy
    ntfy_topic: mcgram-explicit
""",
    )
    dest = s.resolve_destination()
    assert dest.transport == "ntfy"
    assert dest.ntfy_topic == "mcgram-explicit"


def test_ntfy_channel_missing_topic_falls_back_to_default_topic(tmp_path: Path) -> None:
    s = _write(
        tmp_path / "c.yaml",
        """
ntfy:
  default_topic: mcgram-fallback
channels:
  alerts:
    transport: ntfy
""",
    )
    dest = s.resolve_destination("alerts")
    assert dest.ntfy_topic == "mcgram-fallback"


def test_ntfy_channel_no_topic_anywhere_raises(tmp_path: Path) -> None:
    s = _write(
        tmp_path / "c.yaml",
        """
bot:
  operator_chat_id: 1
ntfy:
  server: https://ntfy.sh
channels:
  alerts:
    transport: ntfy
""",
    )
    with pytest.raises(ConfigError, match="no topic"):
        s.resolve_destination("alerts")


def test_telegram_channel_missing_chat_id_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="requires chat_id"):
        _write(
            tmp_path / "c.yaml",
            """
bot:
  operator_chat_id: 1
channels:
  oops:
    transport: telegram
""",
        )


def test_resolve_destination_returns_telegram_destination(tmp_path: Path) -> None:
    s = _write(tmp_path / "c.yaml", "bot:\n  operator_chat_id: 7\n")
    dest = s.resolve_destination()
    assert isinstance(dest, Destination)
    assert dest.transport == "telegram"
    assert dest.chat_id == 7
    assert dest.ntfy_topic is None


def test_resolve_destination_returns_ntfy_destination_with_server(tmp_path: Path) -> None:
    s = _write(
        tmp_path / "c.yaml",
        """
ntfy:
  server: https://ntfy.example.com
  default_topic: mcgram-eee
""",
    )
    dest = s.resolve_destination()
    assert dest.transport == "ntfy"
    assert dest.ntfy_server == "https://ntfy.example.com"


def test_resolve_channel_legacy_raises_on_ntfy_channel(tmp_path: Path) -> None:
    s = _write(
        tmp_path / "c.yaml",
        """
ntfy:
  default_topic: mcgram-x
""",
    )
    with pytest.raises(ConfigError, match="not a telegram channel"):
        s.resolve_channel()


def test_ntfy_access_token_resolved_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NTFY_TOKEN", "tk_xyz")
    s = _write(
        tmp_path / "c.yaml",
        """
ntfy:
  default_topic: mcgram-t
  access_token_env: NTFY_TOKEN
""",
    )
    dest = s.resolve_destination()
    assert dest.ntfy_access_token == "tk_xyz"


def test_ntfy_access_token_env_unset_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NTFY_TOKEN", raising=False)
    s = _write(
        tmp_path / "c.yaml",
        """
ntfy:
  default_topic: mcgram-t
  access_token_env: NTFY_TOKEN
""",
    )
    dest = s.resolve_destination()
    assert dest.ntfy_access_token is None


def test_unknown_channel_raises(tmp_path: Path) -> None:
    s = _write(tmp_path / "c.yaml", "bot:\n  operator_chat_id: 1\n")
    with pytest.raises(ConfigError, match="unknown channel"):
        s.resolve_destination("nope")


def test_ntfy_only_resolve_token_raises_helpfully(tmp_path: Path) -> None:
    s = _write(
        tmp_path / "c.yaml",
        """
ntfy:
  default_topic: mcgram-t
""",
    )
    with pytest.raises(ConfigError, match="no `bot` section"):
        s.resolve_token()


def test_ntfy_invalid_server_url_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="ntfy.server"):
        _write(
            tmp_path / "c.yaml",
            """
ntfy:
  server: not-a-url
  default_topic: x
""",
        )


def test_channel_with_mixed_transports(tmp_path: Path) -> None:
    s = _write(
        tmp_path / "c.yaml",
        """
bot:
  operator_chat_id: 1
ntfy:
  default_topic: mcgram-mixed
channels:
  alerts:
    transport: ntfy
    ntfy_topic: mcgram-alerts
  team:
    chat_id: -1001
""",
    )
    assert s.resolve_destination("alerts").transport == "ntfy"
    assert s.resolve_destination("alerts").ntfy_topic == "mcgram-alerts"
    assert s.resolve_destination("team").transport == "telegram"
    assert s.resolve_destination("team").chat_id == -1001


# --------------------------------------------------------------------------- #
# ntfy.ask (opt-in two-way ask over ntfy)
# --------------------------------------------------------------------------- #

_ASK_BASE = """
ntfy:
  default_topic: mcgram-q-0123abcd
{token}  ask:
    enabled: true
{reply}channels:
  alerts:
    transport: ntfy
    ntfy_topic: mcgram-alerts
"""


def _ask_yaml(
    *, token: bool = True, reply: str | None = "mcgram-r-0123abcd"
) -> str:
    return _ASK_BASE.format(
        token="  access_token_env: NTFY_TOKEN\n" if token else "",
        reply=f"    reply_topic: {reply}\n" if reply is not None else "",
    )


def test_ntfy_ask_is_off_by_default_and_existing_configs_load(tmp_path: Path) -> None:
    s = _write(tmp_path / "c.yaml", "ntfy:\n  default_topic: mcgram-abc123\n")
    assert s.ntfy is not None
    assert s.ntfy.ask.enabled is False
    assert s.ntfy.ask.reply_topic is None


def test_ntfy_ask_enabled_with_token_and_reply_topic_loads(tmp_path: Path) -> None:
    s = _write(tmp_path / "c.yaml", _ask_yaml())
    assert s.ntfy is not None
    assert s.ntfy.ask.enabled is True
    assert s.ntfy.ask.reply_topic == "mcgram-r-0123abcd"


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (_ask_yaml(reply=None), "reply_topic is required"),
        (_ask_yaml(reply="bad/topic"), "reply_topic"),
        (_ask_yaml(reply="x" * 65), "reply_topic"),
        (_ask_yaml(token=False), "access_token_env"),
        (_ask_yaml(reply="mcgram-q-0123abcd"), "must differ"),
        (_ask_yaml(reply="mcgram-alerts"), "must differ"),
    ],
    ids=["missing", "bad-charset", "too-long", "no-token", "is-default-topic",
         "is-channel-topic"],
)
def test_ntfy_ask_enabled_refuses_unsafe_config(
    tmp_path: Path, body: str, match: str
) -> None:
    with pytest.raises(ConfigError, match=match):
        _write(tmp_path / "c.yaml", body)


def test_ntfy_ask_disabled_skips_its_validation(tmp_path: Path) -> None:
    # A half-written `ask:` block must not break a config that leaves it off.
    s = _write(
        tmp_path / "c.yaml",
        "ntfy:\n  default_topic: mcgram-q\n  ask:\n    enabled: false\n"
        "    reply_topic: mcgram-q\n",
    )
    assert s.ntfy is not None and s.ntfy.ask.enabled is False
