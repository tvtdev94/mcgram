"""cli_init tests for ntfy default-topic generation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from mcgram import cli_init
from mcgram.cli_init import _generate_topic, init_config


@pytest.fixture(autouse=True)
def _no_claude_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_init, "_claude_cli_available", lambda: False)


def test_generate_topic_format() -> None:
    t = _generate_topic()
    assert re.fullmatch(r"mcgram-[0-9a-f]{16}", t)


def test_generate_topic_unique_per_call() -> None:
    seen = {_generate_topic() for _ in range(20)}
    assert len(seen) == 20


def test_init_writes_ntfy_topic_into_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    init_config()
    cfg = Path.home() / ".mcgram" / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    topic = data["ntfy"]["default_topic"]
    assert re.fullmatch(r"mcgram-[0-9a-f]{16}", topic)
    # Placeholder should be fully substituted
    assert "{{NTFY_TOPIC}}" not in cfg.read_text(encoding="utf-8")


def test_init_topic_changes_on_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    init_config()
    cfg = Path.home() / ".mcgram" / "config.yaml"
    first = yaml.safe_load(cfg.read_text(encoding="utf-8"))["ntfy"]["default_topic"]
    init_config(force=True)
    second = yaml.safe_load(cfg.read_text(encoding="utf-8"))["ntfy"]["default_topic"]
    assert first != second


def test_init_topic_preserved_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    init_config()
    cfg = Path.home() / ".mcgram" / "config.yaml"
    first = cfg.read_text(encoding="utf-8")
    init_config()  # idempotent
    assert cfg.read_text(encoding="utf-8") == first


def test_init_idempotent_uses_existing_topic_in_next_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On second run (without --force), the topic printed must match the one
    already in config.yaml, NOT a freshly-generated one."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    init_config()
    cfg = Path.home() / ".mcgram" / "config.yaml"
    first_topic = yaml.safe_load(cfg.read_text(encoding="utf-8"))["ntfy"]["default_topic"]
    capsys.readouterr()  # drain first output

    init_config()  # second run — idempotent
    out = capsys.readouterr().out
    assert first_topic in out
    # And the file is unchanged
    assert (
        yaml.safe_load(cfg.read_text(encoding="utf-8"))["ntfy"]["default_topic"]
        == first_topic
    )


def test_init_next_steps_contains_ntfy_quickstart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    init_config()
    out = capsys.readouterr().out
    assert "ntfy" in out.lower()
    assert "subscribe" in out.lower()
    # Topic shown in next-steps
    cfg = Path.home() / ".mcgram" / "config.yaml"
    topic = yaml.safe_load(cfg.read_text(encoding="utf-8"))["ntfy"]["default_topic"]
    assert topic in out
