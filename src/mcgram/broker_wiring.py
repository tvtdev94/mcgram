"""Boot-time wiring for the ask broker: build it, serve it while we own polling.

Ownership comes and goes at runtime (`PollOwnership` promotes and demotes), so
the owner half is not started once and forgotten. A small supervisor watches
`state.ask_registry` — set only while this process holds the poll lock — and
starts or stops an `AskServer` to match. Reading the registry at each tick
rather than capturing it at boot is what keeps a re-promoted instance serving.

The same supervisor sweeps the ask spool while it does NOT own polling, so
orphaned records are cleaned up even on machines where nobody polls.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

from .ask_broker import HEARTBEAT_KEY, HEARTBEAT_KIND, AskBroker
from .ask_server import AskServer
from .poll_ownership import cancel_task
from .spool import Spool

if TYPE_CHECKING:
    from .config import Settings
    from .runtime import AppState

log = logging.getLogger("mcgram.broker")

ASK_NAMESPACE = "asks"


def safe_spool(settings: Settings, namespace: str) -> tuple[Spool | None, str]:
    """(spool, reason) — the spool only when its directory passes the safety probe."""
    spool = Spool(settings.spool_root, namespace)
    ok, reason = spool.probe()
    return (spool, "") if ok else (None, reason)


def start_ask_broker(state: AppState) -> asyncio.Task | None:
    """Attach the client half to `state` and start the owner supervisor.

    Does nothing unless `broker.enabled` (default off). An unsafe spool also
    leaves the broker off: `ask` then behaves exactly as without a broker.
    """
    settings = state.settings
    if not settings.broker.enabled:
        return None
    spool, reason = safe_spool(settings, ASK_NAMESPACE)
    if spool is None:
        log.warning(
            "broker.enabled is set but the spool at %s is unsafe (%s) — broker off; "
            "`ask` only works in the instance that owns Telegram polling.",
            settings.spool_root, reason,
        )
        return None
    cfg = settings.broker
    state.ask_broker = AskBroker(
        spool, settings.lock_path,
        poll_interval_s=cfg.poll_interval_s,
        ttl_pad_s=cfg.request_ttl_pad_s,
        heartbeat_stale_s=cfg.heartbeat_stale_s,
    )
    return asyncio.create_task(serve_while_owner(state, spool), name="mcgram-ask-broker")


async def serve_while_owner(state: AppState, spool: Spool) -> None:
    """Run an `AskServer` exactly while this process owns polling. Cancellable."""
    cfg = state.settings.broker
    registry = None
    server_task: asyncio.Task | None = None
    last_sweep = 0.0
    try:
        while True:
            current = state.ask_registry
            if current is not registry:
                await _stop(server_task, spool)
                server_task, registry = None, current
                if registry is not None and state.client is not None:
                    server = AskServer(
                        spool, registry, state.client, state.rate, state.audit,
                        poll_interval_s=cfg.poll_interval_s,
                        answer_ttl_s=cfg.answer_ttl_s,
                        heartbeat_stale_s=cfg.heartbeat_stale_s,
                        tmp_grace_s=cfg.tmp_grace_s,
                        sweep_interval_s=cfg.sweep_interval_s,
                    )
                    server_task = asyncio.create_task(server.run(), name="mcgram-ask-server")
            now = time.time()
            # The server sweeps on its own schedule while it runs.
            if server_task is None and now - last_sweep >= cfg.sweep_interval_s:
                last_sweep = now
                spool.sweep(now, tmp_grace_s=cfg.tmp_grace_s)
            await asyncio.sleep(cfg.poll_interval_s)
    finally:
        await _stop(server_task, spool)


async def _stop(task: asyncio.Task | None, spool: Spool) -> None:
    """Stop serving and withdraw our heartbeat.

    Leaving the heartbeat behind would advertise a live owner for up to
    `heartbeat_stale_s` after we stopped reading requests, and every non-owner
    `ask` in that window would block until its timeout instead of failing fast.
    """
    if task is None:
        return
    await cancel_task(task)
    hb = spool.read(HEARTBEAT_KEY, HEARTBEAT_KIND)
    if hb is not None and hb.get("pid") == os.getpid():
        spool.remove(HEARTBEAT_KEY, HEARTBEAT_KIND)
