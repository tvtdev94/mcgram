"""Shared application state passed to every tool handler."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .audit import AuditLog
    from .config import Settings
    from .rate_limiter import RateLimiter
    from .tg_client import TelegramClient
    from .update_dispatcher import UpdateDispatcher


@dataclass
class AppState:
    settings: Settings
    client: TelegramClient
    dispatcher: UpdateDispatcher
    rate: RateLimiter
    audit: AuditLog
    # Filled in Phase 3.
    ask_registry: Any | None = None
    reminders: Any | None = None
    # Tool-call counters (debug helper).
    _counters: dict[str, int] = field(default_factory=dict)

    def bump(self, key: str) -> None:
        self._counters[key] = self._counters.get(key, 0) + 1
