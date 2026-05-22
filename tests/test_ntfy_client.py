"""NtfyClient unit tests using pytest-httpx mock transport."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mcgram.errors import AuthError, NtfyError
from mcgram.ntfy_client import NtfyClient

TOPIC = "mcgram-testtopic1234"
URL = f"https://ntfy.sh/{TOPIC}"
HEALTH_URL = "https://ntfy.sh/v1/health"


def _ntfy_ok(text: str = "ok") -> dict:
    return {
        "id": "n123", "time": 1700000000, "event": "message",
        "topic": TOPIC, "message": text,
    }


async def test_send_message_posts_text_to_topic_url(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, method="POST", json=_ntfy_ok("hello"))
    async with NtfyClient() as c:
        r = await c.send_message(TOPIC, "hello")
    assert r["id"] == "n123"
    req = httpx_mock.get_requests()[0]
    assert req.method == "POST"
    assert req.content == b"hello"


async def test_send_message_includes_title_header(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, method="POST", json=_ntfy_ok())
    async with NtfyClient() as c:
        await c.send_message(TOPIC, "x", title="Build")
    req = httpx_mock.get_requests()[0]
    assert req.headers.get("Title") == "Build"


async def test_send_message_priority_one_when_silent(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, method="POST", json=_ntfy_ok())
    async with NtfyClient() as c:
        await c.send_message(TOPIC, "x", silent=True)
    assert httpx_mock.get_requests()[0].headers.get("Priority") == "1"


async def test_send_message_explicit_priority_wins_over_silent(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, method="POST", json=_ntfy_ok())
    async with NtfyClient() as c:
        await c.send_message(TOPIC, "x", silent=True, priority=5)
    assert httpx_mock.get_requests()[0].headers.get("Priority") == "5"


async def test_send_message_tags_joined_comma(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, method="POST", json=_ntfy_ok())
    async with NtfyClient() as c:
        await c.send_message(TOPIC, "x", tags=["warning", "robot"])
    assert httpx_mock.get_requests()[0].headers.get("Tags") == "warning,robot"


async def test_send_message_markdown_header_only_when_true(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, method="POST", json=_ntfy_ok())
    async with NtfyClient() as c:
        await c.send_message(TOPIC, "x", markdown=True)
    assert httpx_mock.get_requests()[0].headers.get("Markdown") == "yes"


async def test_send_message_no_markdown_header_by_default(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, method="POST", json=_ntfy_ok())
    async with NtfyClient() as c:
        await c.send_message(TOPIC, "x")
    assert "Markdown" not in httpx_mock.get_requests()[0].headers


async def test_send_file_puts_binary_with_filename_header(
    httpx_mock, tmp_path: Path
) -> None:
    p = tmp_path / "log.txt"
    p.write_bytes(b"log contents")
    httpx_mock.add_response(url=URL, method="PUT", json=_ntfy_ok())
    async with NtfyClient() as c:
        await c.send_file(TOPIC, p)
    req = httpx_mock.get_requests()[0]
    assert req.method == "PUT"
    assert req.headers.get("Filename") == "log.txt"
    assert req.content == b"log contents"


async def test_send_file_includes_caption_via_message_header(
    httpx_mock, tmp_path: Path
) -> None:
    p = tmp_path / "a.txt"
    p.write_bytes(b"data")
    httpx_mock.add_response(url=URL, method="PUT", json=_ntfy_ok())
    async with NtfyClient() as c:
        await c.send_file(TOPIC, p, caption="here is the log")
    assert httpx_mock.get_requests()[0].headers.get("Message") == "here is the log"


async def test_send_video_same_as_send_file(httpx_mock, tmp_path: Path) -> None:
    p = tmp_path / "demo.mp4"
    p.write_bytes(b"\x00\x00videopayload")
    httpx_mock.add_response(url=URL, method="PUT", json=_ntfy_ok())
    async with NtfyClient() as c:
        await c.send_video(TOPIC, p, caption="recording")
    req = httpx_mock.get_requests()[0]
    assert req.method == "PUT"
    assert req.headers.get("Filename") == "demo.mp4"
    # MIME guess should set Content-Type to video/mp4
    assert req.headers.get("Content-Type") == "video/mp4"


async def test_health_returns_true_on_200(httpx_mock) -> None:
    httpx_mock.add_response(url=HEALTH_URL, method="GET", json={"healthy": True})
    async with NtfyClient() as c:
        assert await c.health() is True


async def test_health_returns_false_on_error(httpx_mock) -> None:
    httpx_mock.add_response(url=HEALTH_URL, method="GET", status_code=503)
    async with NtfyClient() as c:
        assert await c.health() is False


async def test_non_2xx_raises_ntfy_error_with_description(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, method="POST",
                            status_code=429, text="rate limited")
    async with NtfyClient() as c:
        with pytest.raises(NtfyError) as exc:
            await c.send_message(TOPIC, "x")
    assert exc.value.status == 429
    assert "rate limited" in exc.value.description


async def test_401_raises_auth_error(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, method="POST", status_code=401, text="forbidden")
    async with NtfyClient() as c:
        with pytest.raises(AuthError):
            await c.send_message(TOPIC, "x")


async def test_403_raises_auth_error(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, method="POST", status_code=403, text="denied")
    async with NtfyClient() as c:
        with pytest.raises(AuthError):
            await c.send_message(TOPIC, "x")


async def test_client_used_outside_context_raises_runtime_error() -> None:
    c = NtfyClient()
    with pytest.raises(RuntimeError, match="outside"):
        await c.send_message(TOPIC, "x")


async def test_custom_server_url_used_for_all_requests(httpx_mock) -> None:
    custom = "https://ntfy.example.com"
    httpx_mock.add_response(
        url=f"{custom}/{TOPIC}", method="POST", json=_ntfy_ok(),
    )
    async with NtfyClient(server=custom) as c:
        await c.send_message(TOPIC, "x")


async def test_access_token_sets_bearer_header(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, method="POST", json=_ntfy_ok())
    async with NtfyClient(access_token="tk_secret") as c:
        await c.send_message(TOPIC, "x")
    assert httpx_mock.get_requests()[0].headers.get("Authorization") == "Bearer tk_secret"


async def test_non_ascii_header_value_is_replaced(httpx_mock) -> None:
    httpx_mock.add_response(url=URL, method="POST", json=_ntfy_ok())
    async with NtfyClient() as c:
        await c.send_message(TOPIC, "x", title="Xin chào 🚀")
    title = httpx_mock.get_requests()[0].headers.get("Title")
    assert title is not None
    # non-ASCII collapsed to '?' — no httpx encoding crash
    title.encode("ascii")


# Suppress unused-import warning for `httpx`
_ = httpx
