"""Boot wiring for the operator inbox: the session state and its supervisor.

Every session gets an `InboxSession` (label, start time, what it has read).
The intake handler is registered once; the dispatcher only runs while this
process polls, so it stores commands only in the polling session. A supervisor
task beats the inbox heartbeat exactly while `state.owns_polling` is true, and
sweeps expired records in every session.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .broker_wiring import safe_spool
from .errors import ConfigError
from .inbox import InboxStore, make_intake, signing_key

if TYPE_CHECKING:
    from .runtime import AppState

log = logging.getLogger("mcgram.inbox")

INBOX_NAMESPACE = "inbox"
LABEL_ENV = "MCGRAM_SESSION_LABEL"
_LABEL_MAX = 32
_LABEL_FALLBACK = "session"
_NOT_LABEL_RE = re.compile(r"[^a-z0-9_]")
#: Upper bound on the heartbeat period, as for the broker heartbeat.
_HEARTBEAT_MAX_PERIOD_S = 10.0


@dataclass
class InboxSession:
    """This session's view of the inbox. `store` is None when `unavailable` says why.

    `seen` maps each record this session has read (or found forged) to the time
    it stops mattering: the record's own expiry. It is pruned by that time only,
    never by what a directory listing shows — a listing that fails reads as an
    empty inbox, and forgetting on that basis would replay every command.
    """

    label: str
    started_at: float
    store: InboxStore | None
    unavailable: str | None = None
    seen: dict[str, float] = field(default_factory=dict)

    def mark_seen(self, key: str, until: float) -> None:
        self.seen[key] = max(until, self.seen.get(key, until))

    def forget_expired(self, now: float) -> None:
        """Drop entries past their expiry; such a record is never shown again anyway."""
        for key in [k for k, until in self.seen.items() if until < now]:
            del self.seen[key]


def session_label() -> str:
    """`MCGRAM_SESSION_LABEL`, else the working folder name, as `[a-z0-9_]{1,32}`.

    This is the name the operator addresses with `/cmd@label`.
    """
    raw = os.environ.get(LABEL_ENV, "").strip()
    if not raw:
        try:
            raw = Path.cwd().name
        except OSError:  # the working folder was deleted under us
            raw = ""
    label = _NOT_LABEL_RE.sub("_", raw.lower())[:_LABEL_MAX]
    return label or _LABEL_FALLBACK


def start_inbox(state: AppState) -> asyncio.Task[None] | None:
    """Attach `state.inbox` and start the supervisor. None when off or unusable.

    Does nothing unless `inbox.enabled` (default off). When it is on but cannot
    work here, `state.inbox.unavailable` holds the first reason and
    `check_inbox` reports it; no handler is registered and nothing is written.
    """
    settings = state.settings
    if not settings.inbox.enabled:
        return None
    label = session_label()
    store, reason = _build_store(state)
    state.inbox = InboxSession(label=label, started_at=time.time(), store=store,
                               unavailable=reason)
    if store is None:
        log.warning("inbox.enabled is set but the inbox is off here: %s", reason)
        return None
    log.info("operator inbox on; this session's label is %r", label)
    bot = settings.bot
    assert bot is not None  # _build_store refuses a config without one
    state.dispatcher.register(make_intake(store, state.rate, state.audit, bot.operator_chat_id))
    return asyncio.create_task(supervise(state, state.inbox, store), name="mcgram-inbox")


def _build_store(state: AppState) -> tuple[InboxStore | None, str | None]:
    settings = state.settings
    bot = settings.bot
    if state.client is None or bot is None:
        return None, "no Telegram bot is configured on this machine"
    if bot.disable_polling:
        return None, "bot.disable_polling is set, so no session here polls Telegram"
    if bot.operator_chat_id <= 0:
        return None, ("operator_chat_id is a group or channel id; the inbox accepts "
                      "commands from a private chat only")
    try:
        key = signing_key(settings.resolve_token())
    except ConfigError as e:
        return None, str(e)
    spool, unsafe = safe_spool(settings, INBOX_NAMESPACE)
    if spool is None:
        return None, f"the inbox spool is unsafe ({unsafe})"
    return InboxStore(
        spool, key, operator_chat_id=bot.operator_chat_id, ttl_s=settings.inbox.ttl_s,
        heartbeat_stale_s=settings.broker.heartbeat_stale_s,
    ), None


async def supervise(state: AppState, session: InboxSession, store: InboxStore) -> None:
    """Beat while this process polls, sweep always. Cancellable.

    The beat stops, and the heartbeat is withdrawn, as soon as ownership is
    lost: a reader that still saw it would wait for commands nobody stores.
    """
    cfg = state.settings.broker
    period = min(_HEARTBEAT_MAX_PERIOD_S, cfg.heartbeat_stale_s / 6)
    last_beat = -math.inf
    last_sweep = -math.inf
    beating = False
    try:
        while True:
            try:
                now = time.monotonic()
                if state.owns_polling:
                    if now - last_beat >= period and store.beat():
                        last_beat, beating = now, True
                elif beating:
                    store.withdraw()
                    last_beat, beating = -math.inf, False
                if now - last_sweep >= cfg.sweep_interval_s:
                    last_sweep = now
                    store.spool.sweep(tmp_grace_s=cfg.tmp_grace_s)
                    session.forget_expired(time.time())
            except Exception:
                log.exception("inbox supervisor tick failed")
            await asyncio.sleep(cfg.poll_interval_s)
    finally:
        store.withdraw()
