"""DiscordClient unit tests using pytest-httpx mock transport."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from mcgram.discord_client import (
    DiscordClient,
    format_mention_prefix,
    resolve_mentions,
)
from mcgram.errors import DiscordError

WEBHOOK = "https://discord.com/api/webhooks/123456789/abctoken"


def _msg_ok(mid: str = "555") -> dict:
    return {"id": mid, "type": 0, "channel_id": "999", "content": "hi"}


async def test_send_message_posts_json_with_wait(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", json=_msg_ok())
    async with DiscordClient() as c:
        r = await c.send_message(WEBHOOK, "hello")
    assert r["id"] == "555"
    req = httpx_mock.get_requests()[0]
    assert req.method == "POST"
    assert req.url.params.get("wait") == "true"
    body = json.loads(req.content)
    assert body["content"] == "hello"


async def test_thread_id_goes_in_query_not_body(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", json=_msg_ok())
    async with DiscordClient() as c:
        await c.send_message(WEBHOOK, "hi", thread_id="1532959062499659987")
    req = httpx_mock.get_requests()[0]
    # thread_id MUST be a query param, never a body field
    assert req.url.params.get("thread_id") == "1532959062499659987"
    body = json.loads(req.content)
    assert "thread_id" not in body


async def test_no_thread_id_absent_from_query(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", json=_msg_ok())
    async with DiscordClient() as c:
        await c.send_message(WEBHOOK, "hi")
    assert "thread_id" not in httpx_mock.get_requests()[0].url.params


async def test_silent_sets_suppress_flag(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", json=_msg_ok())
    async with DiscordClient() as c:
        await c.send_message(WEBHOOK, "hi", silent=True)
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["flags"] == 4096


async def test_not_silent_omits_flags(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", json=_msg_ok())
    async with DiscordClient() as c:
        await c.send_message(WEBHOOK, "hi")
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert "flags" not in body


async def test_allowed_mentions_defaults_to_parse_empty(httpx_mock) -> None:
    """Every message pings nobody by default — blocks @everyone/@here/roles."""
    httpx_mock.add_response(method="POST", json=_msg_ok())
    async with DiscordClient() as c:
        await c.send_message(WEBHOOK, "hi @everyone")
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["allowed_mentions"] == {"parse": []}


async def test_allowed_mentions_whitelists_given_user_ids(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", json=_msg_ok())
    async with DiscordClient() as c:
        await c.send_message(WEBHOOK, "<@1> <@2> hi", mention_user_ids=["1", "2"])
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["allowed_mentions"] == {"parse": [], "users": ["1", "2"]}


def test_format_mention_prefix() -> None:
    assert format_mention_prefix([]) == ""
    assert format_mention_prefix(["123"]) == "<@123> "
    assert format_mention_prefix(["1", "2"]) == "<@1> <@2> "


def test_resolve_mentions_splits_known_and_unknown() -> None:
    registry = {"alice": "111", "bob": "222"}
    ids, unknown = resolve_mentions(["alice", "ghost", "bob"], registry)
    assert ids == ["111", "222"]
    assert unknown == ["ghost"]


async def test_username_none_omitted_not_null(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", json=_msg_ok())
    async with DiscordClient() as c:
        await c.send_message(WEBHOOK, "hi", username=None, avatar_url=None)
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert "username" not in body
    assert "avatar_url" not in body


async def test_username_and_avatar_included_when_set(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", json=_msg_ok())
    async with DiscordClient() as c:
        await c.send_message(
            WEBHOOK, "hi", username="Tuan Assistant", avatar_url="https://x/a.png"
        )
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["username"] == "Tuan Assistant"
    assert body["avatar_url"] == "https://x/a.png"


async def test_existing_query_on_webhook_url_preserved(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", json=_msg_ok())
    async with DiscordClient() as c:
        await c.send_message(WEBHOOK + "?foo=bar", "hi", thread_id="42")
    params = httpx_mock.get_requests()[0].url.params
    assert params.get("foo") == "bar"
    assert params.get("wait") == "true"
    assert params.get("thread_id") == "42"


async def test_send_file_multipart_has_two_parts(httpx_mock, tmp_path: Path) -> None:
    p = tmp_path / "log.txt"
    p.write_bytes(b"log contents")
    httpx_mock.add_response(method="POST", json=_msg_ok())
    async with DiscordClient() as c:
        await c.send_file(WEBHOOK, p, content="see log", thread_id="42")
    req = httpx_mock.get_requests()[0]
    assert req.url.params.get("thread_id") == "42"
    assert b'name="payload_json"' in req.content
    assert b'name="files[0]"' in req.content
    assert b"log contents" in req.content


async def test_send_video_delegates_to_send_file(httpx_mock, tmp_path: Path) -> None:
    p = tmp_path / "demo.mp4"
    p.write_bytes(b"\x00\x00videopayload")
    httpx_mock.add_response(method="POST", json=_msg_ok())
    async with DiscordClient() as c:
        await c.send_video(WEBHOOK, p)
    req = httpx_mock.get_requests()[0]
    assert b'name="files[0]"' in req.content
    assert b"videopayload" in req.content


async def test_unknown_webhook_maps_to_friendly_reason(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST", status_code=404,
        json={"message": "Unknown Webhook", "code": 10015},
    )
    async with DiscordClient() as c:
        with pytest.raises(DiscordError) as exc:
            await c.send_message(WEBHOOK, "hi")
    assert exc.value.status == 404
    assert exc.value.code == 10015
    assert "deleted" in exc.value.description


async def test_unknown_channel_thread_maps_to_friendly_reason(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST", status_code=400,
        json={"message": "Unknown Channel", "code": 10003},
    )
    async with DiscordClient() as c:
        with pytest.raises(DiscordError) as exc:
            await c.send_message(WEBHOOK, "hi", thread_id="bad")
    assert exc.value.code == 10003
    assert "thread_id" in exc.value.description


async def test_rate_limit_reports_retry_after(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST", status_code=429,
        json={"message": "You are being rate limited.", "retry_after": 0.5},
    )
    async with DiscordClient() as c:
        with pytest.raises(DiscordError) as exc:
            await c.send_message(WEBHOOK, "hi")
    assert exc.value.status == 429
    assert "0.5" in exc.value.description


async def test_error_without_json_body_uses_http_status(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", status_code=500, text="boom")
    async with DiscordClient() as c:
        with pytest.raises(DiscordError) as exc:
            await c.send_message(WEBHOOK, "hi")
    assert exc.value.status == 500
    assert exc.value.code is None


async def test_empty_2xx_body_returns_status(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", status_code=200)
    async with DiscordClient() as c:
        r = await c.send_message(WEBHOOK, "hi")
    assert r == {"status": 200}


async def test_health_returns_metadata_on_200(httpx_mock) -> None:
    httpx_mock.add_response(
        url=WEBHOOK, method="GET",
        json={"name": "hook", "channel_id": "999", "guild_id": "111"},
    )
    async with DiscordClient() as c:
        meta = await c.health(WEBHOOK)
    assert meta is not None
    assert meta["channel_id"] == "999"


async def test_health_returns_none_on_404(httpx_mock) -> None:
    httpx_mock.add_response(
        url=WEBHOOK, method="GET", status_code=404,
        json={"message": "Unknown Webhook", "code": 10015},
    )
    async with DiscordClient() as c:
        assert await c.health(WEBHOOK) is None


async def test_client_used_outside_context_raises_runtime_error() -> None:
    c = DiscordClient()
    with pytest.raises(RuntimeError, match="outside"):
        await c.send_message(WEBHOOK, "hi")


# Suppress unused-import warning for `httpx`
_ = httpx
