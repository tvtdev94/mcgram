"""Settings loader unit tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcgram.config import Settings
from mcgram.errors import ConfigError


def _write_yaml(p: Path, body: str) -> None:
    p.write_text(body, encoding="utf-8")


def test_load_minimal(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    _write_yaml(cfg, "bot:\n  operator_chat_id: 42\n")
    s = Settings.load(cfg)
    assert s.bot.operator_chat_id == 42
    assert s.bot.token_env == "MCGRAM_BOT_TOKEN"
    assert s.defaults.ask_timeout_s == 120
    assert s.limits.file_max_bytes == 52_428_800
    assert s.allow_outside_cwd is False


def test_load_overrides(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    _write_yaml(
        cfg,
        """
bot:
  token_env: MY_TOKEN
  operator_chat_id: 99
defaults:
  ask_timeout_s: 60
  parse_mode: markdown_v2
limits:
  ask_options_max: 4
  file_max_bytes: 1024
allow_outside_cwd: true
""",
    )
    s = Settings.load(cfg)
    assert s.bot.token_env == "MY_TOKEN"
    assert s.defaults.parse_mode == "markdown_v2"
    assert s.limits.ask_options_max == 4
    assert s.allow_outside_cwd is True


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        Settings.load(tmp_path / "missing.yaml")


def test_load_invalid_yaml(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    _write_yaml(cfg, "bot: [unclosed\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        Settings.load(cfg)


def test_load_missing_required_field(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    _write_yaml(cfg, "bot:\n  token_env: T\n")  # no operator_chat_id
    with pytest.raises(ConfigError, match="invalid config"):
        Settings.load(cfg)


def test_load_via_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "from_env.yaml"
    _write_yaml(cfg, "bot:\n  operator_chat_id: 7\n")
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    s = Settings.load()
    assert s.bot.operator_chat_id == 7


def test_resolve_token_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    _write_yaml(cfg, "bot:\n  token_env: MY_BOT_TOKEN\n  operator_chat_id: 1\n")
    monkeypatch.setenv("MY_BOT_TOKEN", "abc:123")
    s = Settings.load(cfg)
    assert s.resolve_token() == "abc:123"


def test_resolve_token_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    _write_yaml(cfg, "bot:\n  token_env: ABSENT_VAR\n  operator_chat_id: 1\n")
    monkeypatch.delenv("ABSENT_VAR", raising=False)
    s = Settings.load(cfg)
    with pytest.raises(ConfigError, match="ABSENT_VAR"):
        s.resolve_token()


def test_audit_path_expanded(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    _write_yaml(
        cfg,
        "bot:\n  operator_chat_id: 1\naudit:\n  path: ~/x.jsonl\n",
    )
    s = Settings.load(cfg)
    assert "~" not in s.audit.path
    assert s.audit.path.endswith(os.path.join("", "x.jsonl"))


def test_retention_days_must_be_positive(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    _write_yaml(
        cfg,
        "bot:\n  operator_chat_id: 1\naudit:\n  retention_days: 0\n",
    )
    with pytest.raises(ConfigError):
        Settings.load(cfg)


def test_lock_path_default(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    audit_path = str(tmp_path / "audit.jsonl").replace("\\", "/")
    _write_yaml(
        cfg,
        f"bot:\n  operator_chat_id: 1\naudit:\n  path: {audit_path}\n",
    )
    s = Settings.load(cfg)
    assert s.lock_path == tmp_path / ".lock"
