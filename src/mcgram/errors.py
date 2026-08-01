"""mcgram exception hierarchy."""

from __future__ import annotations


class MCGramError(Exception):
    """Base exception for all mcgram errors."""


class ConfigError(MCGramError):
    """Configuration loading/validation failure."""


class LockHeldError(MCGramError):
    """Single-instance lock is held by another process."""

    def __init__(self, pid: int, path: str) -> None:
        super().__init__(f"lock held by pid={pid} at {path}")
        self.pid = pid
        self.path = path


class RateLimitError(MCGramError):
    """Rate limit exceeded for a tool."""

    def __init__(self, tool: str) -> None:
        super().__init__(f"rate_limit_exceeded: {tool}")
        self.tool = tool


class AuthError(MCGramError):
    """Telegram authentication / token failure."""


class LimitError(MCGramError):
    """A configured numeric limit was exceeded (file size, options count, etc)."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class TelegramError(MCGramError):
    """Telegram Bot API returned a non-ok response."""

    def __init__(self, status: int, description: str) -> None:
        super().__init__(f"telegram_api: {status} {description}")
        self.status = status
        self.description = description


class NtfyError(MCGramError):
    """ntfy.sh HTTP API returned a non-2xx response."""

    def __init__(self, status: int, description: str) -> None:
        super().__init__(f"ntfy_api: {status} {description}")
        self.status = status
        self.description = description


class DiscordError(MCGramError):
    """Discord webhook API returned a non-2xx response.

    Carries the numeric Discord error `code` (e.g. 10015 Unknown Webhook) when
    present — it is finer-grained than the HTTP status and drives the
    human-readable reason mapping in `DiscordClient`.
    """

    def __init__(self, status: int, description: str, code: int | None = None) -> None:
        super().__init__(f"discord_api: {status} {description}")
        self.status = status
        self.description = description
        self.code = code
