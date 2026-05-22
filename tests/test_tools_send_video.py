"""send_video tool handler tests."""

from __future__ import annotations

from pathlib import Path

from mcgram.runtime import AppState
from mcgram.tools import send_video


async def test_happy_path(app_state: AppState, httpx_mock, tmp_path: Path,
                          monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"\x00\x00\x00\x18ftypmp42")  # not a real mp4 but path-check ok
    httpx_mock.add_response(
        url="https://api.telegram.org/botfake-token/sendVideo",
        json={"ok": True, "result": {"message_id": 42}},
    )
    out = await send_video.handle(app_state, path=str(vid), caption="demo")
    assert out["ok"] is True
    assert out["message_id"] == 42
    assert out["channel"] == "default"


async def test_rejects_non_video_extension(
    app_state: AppState, tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    txt = tmp_path / "notes.txt"
    txt.write_text("hi")
    out = await send_video.handle(app_state, path=str(txt))
    assert out["error"] == "invalid_input"
    assert out["reason"] == "not_a_video"
    assert ".mp4" in out["allowed"]


async def test_path_outside_cwd_rejected(
    app_state: AppState, tmp_path: Path, monkeypatch,
) -> None:
    inside = tmp_path / "inside"
    inside.mkdir()
    monkeypatch.chdir(inside)
    vid = tmp_path / "outside.mp4"
    vid.write_bytes(b"\x00")
    out = await send_video.handle(app_state, path=str(vid))
    assert out["error"] == "rejected"
    assert out["reason"] == "path_outside_cwd"


async def test_unknown_channel(
    app_state: AppState, tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    vid = tmp_path / "x.mp4"
    vid.write_bytes(b"\x00")
    out = await send_video.handle(app_state, path=str(vid), channel="missing")
    assert out["error"] == "invalid_input"
    assert out["reason"] == "unknown_channel"


def test_schema() -> None:
    s = send_video.schema()
    assert s["name"] == "send_video"
    assert "path" in s["inputSchema"]["required"]
    assert "supports_streaming" in s["inputSchema"]["properties"]
