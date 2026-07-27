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
    # ask_registry is set ONLY while this process owns the Telegram poll loop —
    # resolving an ask needs the inbound update that only the poller receives.
    ask_registry: Any | None = None
    reminders: Any | None = None
    # Telegram poll-loop ownership. Multiple mcgram processes may run at once
    # (one per Claude Code session), but `getUpdates` allows a single client per
    # bot token, so exactly one of them polls. The others run send-only: every
    # tool works except `ask`. `poll_owner_pid` is the pid holding the lock,
    # surfaced in `ask`'s error so the user knows which session can answer.
    owns_polling: bool = False
    poll_owner_pid: int | None = None
    # Tool-call counters (debug helper).
    _counters: dict[str, int] = field(default_factory=dict)

    def bump(self, key: str) -> None:
        self._counters[key] = self._counters.get(key, 0) + 1
