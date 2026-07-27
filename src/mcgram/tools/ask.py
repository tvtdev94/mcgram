"""Tool: ask — post a question, await answer (button/freetext) or timeout.

Telegram-only: ntfy.sh has no 2-way input. Calls on ntfy channels return
`transport_unsupported` without contacting any backend.

Also requires that THIS process owns the Telegram poll loop. Only the polling
process receives the operator's tap, so a non-owner would post the question and
then block until timeout while the answer lands somewhere else. That is worse
than an error, so `ask` refuses up front — see `_polling_error`.
"""

from __future__ import annotations

import time
from typing import Any

from ..errors import ConfigError, RateLimitError, TelegramError
from ..runtime import AppState

TOOL_NAME = "ask"


def schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Post a question to a configured Telegram channel and wait for a reply. "
            "Returns {value, source: button|freetext|timeout, question_id}. "
            "If options provided, posts inline buttons (1 per row). "
            "Blocks the MCP call until reply or timeout — keep timeout_s short. "
            "REQUIRES a telegram channel; ntfy channels are not supported."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Button labels (omit for freetext-only)",
                },
                "timeout_s": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Override default ask_timeout_s",
                },
                "channel": {
                    "type": "string",
                    "description": "Named channel from config (must be telegram)",
                },
            },
            "required": ["question"],
        },
    }


async def handle(
    state: AppState,
    *,
    question: str,
    options: list[str] | None = None,
    timeout_s: int | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    if not question or not isinstance(question, str):
        return {"error": "invalid_input", "reason": "question_required"}
    limits = state.settings.limits
    if options is not None:
        if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
            return {"error": "invalid_input", "reason": "options_must_be_list_of_str"}
        if len(options) == 0:
            options = None
        elif len(options) > limits.ask_options_max:
            return {
                "error": "invalid_input", "reason": "too_many_options",
                "max": limits.ask_options_max,
            }
    effective_timeout = timeout_s or state.settings.defaults.ask_timeout_s
    if effective_timeout > limits.ask_timeout_max_s:
        effective_timeout = limits.ask_timeout_max_s

    try:
        dest = state.settings.resolve_destination(channel)
    except ConfigError as e:
        state.audit.write({"tool": TOOL_NAME, "status": "rejected",
                           "reason": "unknown_channel", "channel": channel})
        return {"error": "invalid_input", "reason": "unknown_channel", "detail": str(e)}

    if dest.transport != "telegram":
        state.audit.write({
            "tool": TOOL_NAME, "status": "rejected",
            "reason": "transport_unsupported",
            "channel": dest.name, "transport": dest.transport,
        })
        return {
            "error": "transport_unsupported",
            "reason": "ask requires a telegram channel (ntfy.sh has no 2-way input)",
            "channel": dest.name,
            "hint": "use send_message on this channel, or switch to a telegram channel",
        }

    if state.ask_registry is None or state.client is None:
        return _polling_error(state, dest.name)
    assert dest.chat_id is not None
    if not state.rate.try_acquire(TOOL_NAME):
        state.audit.write({"tool": TOOL_NAME, "status": "rejected", "reason": "rate_limit"})
        raise RateLimitError(TOOL_NAME)

    # Capture the registry: ownership (and with it `state.ask_registry`) can be
    # dropped by the supervisor while this call is in flight.
    registry = state.ask_registry
    t0 = time.monotonic()
    try:
        result = await registry.open(
            state.client,
            chat_id=dest.chat_id,
            question=question,
            options=options,
            timeout_s=effective_timeout,
        )
    except TelegramError as e:
        state.audit.write({
            "tool": TOOL_NAME, "status": "error", "error": e.description,
        })
        return {"error": "telegram_api", "reason": e.description}
    ms = int((time.monotonic() - t0) * 1000)
    state.audit.write({
        "tool": TOOL_NAME, "status": "ok",
        "channel": dest.name, "transport": "telegram",
        "question_id": result["question_id"],
        "source": result["source"],
        "ms": ms,
    })
    result["channel"] = dest.name
    return result


def _polling_error(state: AppState, channel: str) -> dict[str, Any]:
    """Explain why `ask` can't run here — three distinct causes, three messages.

    Returns immediately (no network, no waiting): the caller would otherwise
    block for the full timeout and then get `source: "timeout"`, which reads as
    "the operator ignored you" when in truth the question never reached anyone
    who could answer it.
    """
    reason, detail, hint = _polling_error_detail(state)
    record = {"tool": TOOL_NAME, "status": "rejected",
              "reason": reason, "channel": channel}
    if state.poll_owner_pid is not None:
        record["poll_owner_pid"] = state.poll_owner_pid
    state.audit.write(record)
    payload: dict[str, Any] = {
        "error": reason, "reason": detail, "channel": channel, "hint": hint,
    }
    if state.poll_owner_pid is not None:
        payload["poll_owner_pid"] = state.poll_owner_pid
    return payload


def _polling_error_detail(state: AppState) -> tuple[str, str, str]:
    if state.client is None:
        return (
            "telegram_not_configured",
            "ask requires a Telegram bot; this instance has no `bot` section in config",
            "add a `bot:` section to ~/.mcgram/config.yaml, or use send_message over ntfy",
        )
    if state.settings.bot is not None and state.settings.bot.disable_polling:
        return (
            "polling_disabled",
            "ask requires Telegram long-polling, disabled here via bot.disable_polling",
            "set bot.disable_polling = false on a machine that can reach api.telegram.org",
        )
    owner = state.poll_owner_pid
    where = f"another mcgram instance (pid={owner})" if owner else "another mcgram instance"
    return (
        "polling_not_owned",
        f"ask requires the Telegram poll loop, currently owned by {where}. "
        "Only one process per bot token may poll, so only that instance can "
        "receive your reply",
        f"ask from the Claude Code session running {where}, or use send_message here "
        "(it works in every instance)",
    )
