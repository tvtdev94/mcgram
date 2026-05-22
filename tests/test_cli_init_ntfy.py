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


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Don't hit ntfy.sh from the test suite; default to skipping the welcome seed."""
    monkeypatch.setenv("MCGRAM_INIT_NO_WELCOME", "1")


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


def test_init_seeds_welcome_message_on_fresh_scaffold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MCGRAM_INIT_NO_WELCOME", raising=False)
    posted: dict = {}

    class _FakeResp:
        status_code = 200

    def _fake_post(url: str, **kwargs):  # type: ignore[no-untyped-def]
        posted["url"] = url
        posted["body"] = kwargs.get("content")
        posted["headers"] = kwargs.get("headers")
        return _FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "post", _fake_post)

    init_config()
    out = capsys.readouterr().out
    cfg = Path.home() / ".mcgram" / "config.yaml"
    topic = yaml.safe_load(cfg.read_text(encoding="utf-8"))["ntfy"]["default_topic"]
    assert posted["url"] == f"https://ntfy.sh/{topic}"
    assert b"mcgram installed" in posted["body"]
    assert posted["headers"]["Title"] == "mcgram welcome"
    assert "seeded" in out


def test_init_does_not_seed_when_config_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MCGRAM_INIT_NO_WELCOME", raising=False)
    init_config()  # first scaffold — we don't care about its seed call
    calls = 0

    def _fake_post(*_a, **_kw):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        class R:
            status_code = 200
        return R()

    import httpx
    monkeypatch.setattr(httpx, "post", _fake_post)
    init_config()  # idempotent rerun — must NOT re-post
    assert calls == 0


def test_init_seed_silent_on_network_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MCGRAM_INIT_NO_WELCOME", raising=False)

    import httpx
    def _boom(*_a, **_kw):  # type: ignore[no-untyped-def]
        raise httpx.ConnectError("DNS lookup failed")
    monkeypatch.setattr(httpx, "post", _boom)

    # Must not raise — init succeeds even when ntfy is unreachable.
    rc = init_config()
    assert rc == 0
    out = capsys.readouterr().out
    assert "warning" in out.lower()


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
