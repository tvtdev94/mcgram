"""CLI tests for Discord: `channel add-discord`, `channel list`, doctor, .env writes."""

from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path

import pytest
import yaml

from mcgram import cli_channel, cli_doctor
from mcgram.env_file import upsert_env_var

WEBHOOK = "https://discord.com/api/webhooks/123456789/tok_secret_abc"


def _cfg(tmp_path: Path, body: str = "bot:\n  operator_chat_id: 1\n") -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# env_file.upsert_env_var
# --------------------------------------------------------------------------- #


def test_upsert_creates_file(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    upsert_env_var(p, "A", "1")
    assert p.read_text(encoding="utf-8") == "A=1\n"


def test_upsert_updates_in_place_no_duplicate(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("A=1\nB=2\n", encoding="utf-8")
    upsert_env_var(p, "A", "9")
    txt = p.read_text(encoding="utf-8")
    assert "A=9" in txt and "B=2" in txt
    assert txt.count("A=") == 1


def test_upsert_preserves_other_lines(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("# credentials\nMCGRAM_BOT_TOKEN=tok\n", encoding="utf-8")
    upsert_env_var(p, "MCGRAM_DISCORD_WEBHOOK_EVE", WEBHOOK)
    txt = p.read_text(encoding="utf-8")
    assert "# credentials" in txt
    assert "MCGRAM_BOT_TOKEN=tok" in txt
    assert f"MCGRAM_DISCORD_WEBHOOK_EVE={WEBHOOK}" in txt


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_env_file_is_0600(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    upsert_env_var(p, "K", "v")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


# --------------------------------------------------------------------------- #
# add-discord
# --------------------------------------------------------------------------- #


def test_add_discord_writes_env_and_config(
    tmp_path: Path, httpx_mock, capsys: pytest.CaptureFixture[str]
) -> None:
    httpx_mock.add_response(
        url=WEBHOOK, method="GET",
        json={"name": "eve-hook", "channel_id": "999", "guild_id": "111"},
    )
    cfg = _cfg(tmp_path)
    (tmp_path / ".env").write_text("MCGRAM_BOT_TOKEN=keepme\n", encoding="utf-8")
    rc = cli_channel._add_discord(
        name="eve", webhook=WEBHOOK, env_name=None,
        description=None, force=False, config_path=cfg,
    )
    assert rc == 0
    env_txt = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "MCGRAM_BOT_TOKEN=keepme" in env_txt  # unrelated line preserved
    assert f"MCGRAM_DISCORD_WEBHOOK_EVE={WEBHOOK}" in env_txt
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["channels"]["eve"]["transport"] == "discord"
    assert data["channels"]["eve"]["discord_webhook_env"] == "MCGRAM_DISCORD_WEBHOOK_EVE"
    assert data["discord"]["username"] == "mcgram"
    out = capsys.readouterr().out
    assert WEBHOOK not in out  # URL never echoed
    assert "999" in out  # channel id confirmed to the user


def test_add_discord_custom_env_name(
    tmp_path: Path, httpx_mock, capsys: pytest.CaptureFixture[str]
) -> None:
    httpx_mock.add_response(url=WEBHOOK, method="GET", json={"channel_id": "5"})
    cfg = _cfg(tmp_path)
    rc = cli_channel._add_discord(
        name="eve", webhook=WEBHOOK, env_name="MY_HOOK",
        description="Eve log", force=False, config_path=cfg,
    )
    assert rc == 0
    assert "MY_HOOK=" in (tmp_path / ".env").read_text(encoding="utf-8")
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["channels"]["eve"]["discord_webhook_env"] == "MY_HOOK"


def test_add_discord_bad_url_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _cfg(tmp_path)
    rc = cli_channel._add_discord(
        name="eve", webhook="https://evil.example/x", env_name=None,
        description=None, force=False, config_path=cfg,
    )
    assert rc == 2
    assert not (tmp_path / ".env").exists()
    assert "channels" not in yaml.safe_load(cfg.read_text(encoding="utf-8"))


def test_add_discord_rejected_webhook_writes_nothing(
    tmp_path: Path, httpx_mock
) -> None:
    httpx_mock.add_response(
        url=WEBHOOK, method="GET", status_code=404,
        json={"message": "Unknown Webhook", "code": 10015},
    )
    cfg = _cfg(tmp_path)
    rc = cli_channel._add_discord(
        name="eve", webhook=WEBHOOK, env_name=None,
        description=None, force=False, config_path=cfg,
    )
    assert rc == 1
    assert not (tmp_path / ".env").exists()
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "eve" not in (data.get("channels") or {})


def test_add_discord_default_name_rejected(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    rc = cli_channel._add_discord(
        name="default", webhook=WEBHOOK, env_name=None,
        description=None, force=False, config_path=cfg,
    )
    assert rc == 2


def test_add_discord_existing_needs_force(tmp_path: Path) -> None:
    # The exists check returns before any network probe, so no webhook mock here.
    cfg = _cfg(
        tmp_path,
        "channels:\n  eve:\n    transport: discord\n"
        "    discord_webhook_env: OLD\n",
    )
    rc = cli_channel._add_discord(
        name="eve", webhook=WEBHOOK, env_name=None,
        description=None, force=False, config_path=cfg,
    )
    assert rc == 1  # already exists, no --force


def test_cmd_add_discord_prompts_when_no_webhook(
    tmp_path: Path, httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(url=WEBHOOK, method="GET", json={"channel_id": "5"})
    cfg = _cfg(tmp_path)
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    monkeypatch.setattr(cli_channel.getpass, "getpass", lambda *a, **k: WEBHOOK)
    ns = argparse.Namespace(
        name="eve", webhook=None, env_name=None, description=None, force=False
    )
    assert cli_channel.cmd_add_discord(ns) == 0


# --------------------------------------------------------------------------- #
# channel list
# --------------------------------------------------------------------------- #


def test_list_shows_discord_without_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _cfg(
        tmp_path,
        "channels:\n  eve:\n    transport: discord\n"
        "    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_EVE\n",
    )
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    cli_channel.cmd_list(argparse.Namespace())
    out = capsys.readouterr().out
    assert "eve" in out
    assert "discord" in out
    assert "env=MCGRAM_DISCORD_WEBHOOK_EVE" in out
    assert "webhooks/" not in out


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def test_doctor_checks_discord_no_url_leak(
    tmp_path: Path, httpx_mock, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MCGRAM_DISCORD_WEBHOOK_EVE", WEBHOOK)
    cfg = _cfg(
        tmp_path,
        "channels:\n  eve:\n    transport: discord\n"
        "    discord_webhook_env: MCGRAM_DISCORD_WEBHOOK_EVE\n",
    )
    monkeypatch.setenv("MCGRAM_CONFIG", str(cfg))
    httpx_mock.add_response(url=WEBHOOK, method="GET", json={"channel_id": "5", "name": "h"})
    httpx_mock.add_response(method="POST", json={"id": "777"})
    rc = cli_doctor.doctor()
    out = capsys.readouterr().out
    assert "discord eve" in out
    assert WEBHOOK not in out
    assert "tok_secret_abc" not in out
    assert rc == 0
