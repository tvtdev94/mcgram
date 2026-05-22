"""send_file tool handler tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

from mcgram.runtime import AppState
from mcgram.tools import send_file


def _audit(p: str | Path) -> list[dict]:
    path = Path(p)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def test_happy_path(app_state: AppState, httpx_mock, tmp_path: Path,
                          monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "report.txt"
    target.write_text("hello", encoding="utf-8")
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/sendDocument",
        json={"ok": True, "result": {"message_id": 7}},
    )
    out = await send_file.handle(app_state, path=str(target), caption="cap")
    assert out["ok"] is True
    assert out["message_id"] == 7
    assert out["bytes"] == 5


async def test_file_not_found(app_state: AppState, tmp_path: Path) -> None:
    out = await send_file.handle(app_state, path=str(tmp_path / "missing.txt"))
    assert out["error"] == "invalid_input"
    assert out["reason"] == "file_not_found"


async def test_path_required(app_state: AppState) -> None:
    out = await send_file.handle(app_state, path="")
    assert out["error"] == "invalid_input"
    assert out["reason"] == "path_required"


async def test_not_a_file(app_state: AppState, tmp_path: Path) -> None:
    out = await send_file.handle(app_state, path=str(tmp_path))
    assert out["error"] == "invalid_input"
    assert out["reason"] == "not_a_file"


async def test_too_large(app_state: AppState, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 1024)
    # Patch limits to a tiny cap
    app_state.settings.limits.file_max_bytes = 100
    out = await send_file.handle(app_state, path=str(big))
    assert out["error"] == "rejected"
    assert out["reason"] == "file_too_large"
    assert out["bytes"] == 1024


async def test_path_outside_cwd(app_state: AppState, tmp_path: Path, monkeypatch) -> None:
    # Working dir = tmp_path/inside; target = tmp_path/outside.txt → outside
    inside = tmp_path / "inside"
    inside.mkdir()
    monkeypatch.chdir(inside)
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    out = await send_file.handle(app_state, path=str(outside))
    assert out["error"] == "rejected"
    assert out["reason"] == "path_outside_cwd"


async def test_allow_outside_cwd(app_state: AppState, tmp_path: Path, monkeypatch,
                                  httpx_mock) -> None:
    inside = tmp_path / "inside"
    inside.mkdir()
    monkeypatch.chdir(inside)
    outside = tmp_path / "outside.txt"
    outside.write_text("ok")
    app_state.settings.allow_outside_cwd = True
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/sendDocument",
        json={"ok": True, "result": {"message_id": 1}},
    )
    out = await send_file.handle(app_state, path=str(outside))
    assert out["ok"] is True


async def test_caption_truncated(app_state: AppState, tmp_path: Path, monkeypatch,
                                  httpx_mock) -> None:
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "f.txt"
    p.write_text("x")
    app_state.settings.limits.caption_max_chars = 10
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/sendDocument",
        json={"ok": True, "result": {"message_id": 1}},
    )
    long = "y" * 50
    await send_file.handle(app_state, path=str(p), caption=long)
    body = httpx_mock.get_requests()[0].content
    # Truncated caption should fit limit
    assert b"y" * 50 not in body


def test_schema() -> None:
    s = send_file.schema()
    assert s["name"] == "send_file"
    assert "path" in s["inputSchema"]["required"]


# Silence "unused" import on bare-bones environments
_ = os
