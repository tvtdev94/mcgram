"""Tool: ask — post a question, await answer (button/freetext) or timeout.

Telegram-only: ntfy.sh has no 2-way input. Calls on ntfy channels return
`transport_unsupported` without contacting any backend.

Only the process owning the Telegram poll loop receives the operator's tap. A
non-owner that posted the question itself would block until timeout while the
answer lands elsewhere — worse than an error. So a non-owner either hands the
question to the owner through the opt-in broker (`ask_via_broker`), or refuses
up front — see `_ask_refusal`.
"""

from __future__ import annotations

import time
from typing import Any

from ..ask_via_broker import ask_via_broker
from ..errors import ConfigError, RateLimitError, TelegramError, TransportError
from ..runtime import AppState
from ._ask_refusal import refusal, refusal_payload

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
            "reason": (
                f"ask requires a telegram channel — {dest.transport} is one-way "
                "and has no reply path"
            ),
            "channel": dest.name,
            "hint": "use send_message on this channel, or switch to a telegram channel",
        }

    if state.ask_registry is None or state.client is None:
        why = refusal(state)
        if why is not None:
            return refusal_payload(state, dest.name, why)
        return await ask_via_broker(state, dest, question=question, options=options,
                                    timeout_s=effective_timeout)
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
    except TransportError as e:
        state.audit.write({
            "tool": TOOL_NAME, "status": "error",
            "reason": "transport_unreachable", "error": e.detail,
        })
        return {"error": "transport_unreachable", "transport": e.transport,
                "reason": e.detail,
                "hint": "question was NOT posted — check network connectivity"}
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
