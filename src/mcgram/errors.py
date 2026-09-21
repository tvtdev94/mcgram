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


class TransportError(MCGramError):
    """An HTTP call failed before any response arrived (DNS, connect, timeout).

    Distinct from `TelegramError` / `NtfyError` / `DiscordError`, which all carry
    a real HTTP status: here the request never completed, so nothing was
    delivered. Tools must surface this instead of letting the raw httpx error
    escape as a generic `internal` — a notification that silently failed to send
    is the one failure this tool cannot afford to hide.
    """

    def __init__(self, transport: str, detail: str) -> None:
        super().__init__(f"{transport}_unreachable: {detail}")
        self.transport = transport
        self.detail = detail

    @classmethod
    def from_httpx(
        cls, transport: str, exc: Exception, *secrets: str | None
    ) -> TransportError:
        """Build from an httpx error, scrubbing credentials out of the message.

        httpx errors can embed the request URL, and both the Telegram base URL
        (`/bot<TOKEN>/`) and a Discord webhook URL carry a credential — those
        would otherwise land in the audit log and the tool result.
        """
        detail = f"{type(exc).__name__}: {exc}".strip().removesuffix(":").strip()
        for secret in secrets:
            if secret:
                detail = detail.replace(secret, "***")
        return cls(transport, detail)


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
