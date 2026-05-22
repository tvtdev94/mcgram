"""Shared application state passed to every tool handler."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .audit import AuditLog
    from .config import Settings
    from .ntfy_client import NtfyClient
    from .rate_limiter import RateLimiter
    from .tg_client import TelegramClient
    from .update_dispatcher import UpdateDispatcher


@dataclass
class AppState:
    """Container for runtime singletons. Each transport client is optional —
    presence reflects which transports were configured."""

    settings: Settings
    dispatcher: UpdateDispatcher
    rate: RateLimiter
    audit: AuditLog
    # Telegram (None when no `bot` section in config)
    client: TelegramClient | None = None
    # ntfy.sh (None when no `ntfy` section in config)
    ntfy_client: NtfyClient | None = None
    # Filled in Phase 3.
    ask_registry: Any | None = None
    reminders: Any | None = None
    # Tool-call counters (debug helper).
    _counters: dict[str, int] = field(default_factory=dict)

    def bump(self, key: str) -> None:
        self._counters[key] = self._counters.get(key, 0) + 1
