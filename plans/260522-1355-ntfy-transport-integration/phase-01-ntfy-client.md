# Phase 01 — NtfyClient HTTP wrapper

**Priority:** P0 — blocks all subsequent phases
**Status:** PENDING
**Est. LOC:** ~120

## Overview

Thin async HTTP client cho ntfy.sh, song song `TelegramClient`. Single responsibility: PUT/POST sang `<server>/<topic>` với headers chuẩn ntfy. Không state, không poll, không 2-chiều.

## ntfy.sh API surface (cần)

| Action | HTTP | URL | Body | Headers |
|--------|------|-----|------|---------|
| Send text | POST | `<server>/<topic>` | text | `Title`, `Priority`, `Tags`, `Markdown` |
| Send file | PUT | `<server>/<topic>` | binary | `Filename`, `Message` (caption), `Title` |
| Send video | PUT | `<server>/<topic>` | binary | `Filename` (.mp4 etc.), `Message` |
| Health check | GET | `<server>/v1/health` | — | — |

Auth (deferred to phase 5): `Authorization: Bearer <token>` nếu cần.

## API

```python
class NtfyClient:
    def __init__(self, server: str = "https://ntfy.sh", access_token: str | None = None) -> None: ...
    async def __aenter__(self) -> NtfyClient: ...
    async def __aexit__(self, ...) -> None: ...

    async def send_message(
        self, topic: str, text: str, *,
        title: str | None = None,
        priority: int | None = None,    # 1..5 (default 3)
        tags: list[str] | None = None,
        markdown: bool = False,
        silent: bool = False,            # priority=1 maps to "min" → no sound
    ) -> dict[str, Any]:
        """Return parsed ntfy response (id, time, etc.). Raises NtfyError on non-2xx."""

    async def send_file(
        self, topic: str, path: Path, *,
        caption: str | None = None,
        silent: bool = False,
    ) -> dict[str, Any]:
        """PUT binary body with Filename header."""

    async def send_video(
        self, topic: str, path: Path, *,
        caption: str | None = None,
        silent: bool = False,
    ) -> dict[str, Any]:
        """Same as send_file — ntfy không phân biệt content-type cho playback;
        mobile app sẽ inline-play nếu MIME là video/*."""

    async def health(self) -> bool:
        """GET /v1/health. Return True if 200."""
```

## Errors

Add `NtfyError(MCGramError)` ở `errors.py` với fields `status_code: int`, `description: str`. Map HTTP 401/403 → `AuthError` (parallel with Telegram).

## Limits

- File size: ntfy.sh public limit ~15 MB. Bypass check ở client level (server sẽ reject 413). Audit log đầy đủ.
- Rate limit: ntfy.sh public áp 60 req/h/IP cho free → user phải tự chú ý. Không add internal limit ở phase này (rely on `RateLimiter` đã có).

## Files

- `src/mcgram/ntfy_client.py` — new
- `src/mcgram/errors.py` — add `NtfyError`

## Tests (`tests/unit/test_ntfy_client.py`)

Dùng `respx` (đã có trong test deps, kiểm tra) hoặc `httpx_mock`:

- [ ] `test_send_message_posts_to_topic_url`
- [ ] `test_send_message_includes_title_header_when_provided`
- [ ] `test_send_message_priority_1_when_silent`
- [ ] `test_send_message_markdown_header_only_when_true`
- [ ] `test_send_file_puts_binary_with_filename_header`
- [ ] `test_send_file_includes_caption_via_message_header`
- [ ] `test_send_video_same_as_send_file`
- [ ] `test_health_returns_true_on_200`
- [ ] `test_non_2xx_raises_ntfy_error_with_description`
- [ ] `test_401_raises_auth_error`
- [ ] `test_client_used_outside_context_raises_runtime_error`
- [ ] `test_custom_server_url_used_for_all_requests`

## Acceptance

- All tests pass
- ruff clean
- ≤120 LOC (file budget)
- No mutation of args
