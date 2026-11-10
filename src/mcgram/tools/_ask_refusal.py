"""Why `ask` cannot run in this instance — or None when the broker can carry it.

Every refusal returns immediately (no network, no waiting). The alternative is
blocking for the full timeout and then reporting `source: "timeout"`, which
reads as "the operator ignored you" when in truth the question never reached
anyone who could answer it.
"""

from __future__ import annotations

from typing import Any

from ..runtime import AppState

TOOL_NAME = "ask"

# (error code, detail, hint, broker reason or None)
Refusal = tuple[str, str, str, str | None]


def refusal(state: AppState) -> Refusal | None:
    """Three distinct causes, three messages. None: use the broker."""
    if state.client is None:
        return (
            "telegram_not_configured",
            "ask requires a Telegram bot; this instance has no `bot` section in config",
            "add a `bot:` section to ~/.mcgram/config.yaml, or use send_message over ntfy",
            None,
        )
    if state.settings.bot is not None and state.settings.bot.disable_polling:
        return (
            "polling_disabled",
            "ask requires Telegram long-polling, disabled here via bot.disable_polling",
            "set bot.disable_polling = false on a machine that can reach api.telegram.org",
            None,
        )
    broker = None
    if state.ask_broker is not None:
        alive, broker = state.ask_broker.owner_alive()
        if alive:
            return None
    owner = state.poll_owner_pid
    where = f"another mcgram instance (pid={owner})" if owner else "another mcgram instance"
    return (
        "polling_not_owned",
        f"ask requires the Telegram poll loop, currently owned by {where}. "
        "Only one process per bot token may poll, so only that instance can "
        "receive your reply",
        f"ask from the Claude Code session running {where}, or use send_message here "
        "(it works in every instance)",
        broker,
    )


def refusal_payload(state: AppState, channel: str, why: Refusal) -> dict[str, Any]:
    """Audit the refusal and build the tool result."""
    reason, detail, hint, broker = why
    record: dict[str, Any] = {"tool": TOOL_NAME, "status": "rejected",
                              "reason": reason, "channel": channel}
    payload: dict[str, Any] = {
        "error": reason, "reason": detail, "channel": channel, "hint": hint,
    }
    if state.poll_owner_pid is not None:
        record["poll_owner_pid"] = payload["poll_owner_pid"] = state.poll_owner_pid
    if broker is not None:
        payload["broker"] = broker
    state.audit.write(record)
    return payload
