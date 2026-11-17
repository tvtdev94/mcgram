"""`ask` on an ntfy channel (only with `ntfy.ask.enabled`).

Every refusal here happens before any network call, and every error says
whether the question reached the phone. The Telegram poll-ownership refusals
never apply: each session subscribes to the reply topic on its own.
"""

from __future__ import annotations

import time
from typing import Any

from ..config import Destination
from ..errors import AuthError, NtfyError, RateLimitError, TransportError
from ..ntfy_ask import (
    MAX_OPTIONS,
    NtfyAsk,
    NtfyAskError,
    NtfyAuthFailedError,
    NtfyPostUnconfirmedError,
    NtfyReplyUnreachableError,
    NtfyTopicNotProtectedError,
)
from ..runtime import AppState

TOOL_NAME = "ask"
#: Past this size ntfy turns the message into an attachment.
MAX_QUESTION_BYTES = 4096
_NOT_POSTED = "question was NOT posted"
_SETUP = "see the ntfy section of docs/security-threat-model.md"


async def ask_ntfy(
    state: AppState,
    dest: Destination,
    *,
    question: str,
    options: list[str],
    timeout_s: int,
    multi_select: bool,
    columns: int,
) -> dict[str, Any]:
    registry: NtfyAsk | None = state.ntfy_ask
    assert registry is not None and dest.ntfy_topic is not None

    def refuse(error: str, **fields: Any) -> dict[str, Any]:
        # `invalid_input` carries its code in `reason`; the others are the code.
        code = fields["reason"] if error == "invalid_input" else error
        state.audit.write({"tool": TOOL_NAME, "status": "rejected", "reason": code,
                           "channel": dest.name, "transport": "ntfy"})
        return {"error": error, **fields, "channel": dest.name}

    if multi_select:
        return refuse("invalid_input", reason="multi_select_unsupported", transport="ntfy",
                      hint="ntfy has no toggle buttons; ask one single-choice question")
    max_options = min(state.settings.limits.ask_options_max, MAX_OPTIONS)
    if len(options) > max_options:
        return refuse("invalid_input", reason="too_many_options", max=max_options,
                      hint="ntfy allows at most three buttons per notification")
    if len(question.encode("utf-8")) > MAX_QUESTION_BYTES:
        return refuse("invalid_input", reason="question_too_long",
                      max_bytes=MAX_QUESTION_BYTES)
    if state.ntfy_client is None or not state.ntfy_client.has_token:
        return refuse("ntfy_ask_requires_token",
                      reason="ask over ntfy needs the access token, and it is not set",
                      hint=f"set the ntfy.access_token_env variable in ~/.mcgram/.env; "
                           f"{_NOT_POSTED}")
    if not (dest.ntfy_server or "").startswith("https://"):
        return refuse("ntfy_ask_requires_https",
                      reason="ask over ntfy requires an https:// server: the answer tokens "
                             "and the access token must not travel in clear",
                      hint=f"point ntfy.server at an https:// URL; {_NOT_POSTED}")
    if not state.rate.try_acquire(TOOL_NAME):
        state.audit.write({"tool": TOOL_NAME, "status": "rejected", "reason": "rate_limit"})
        raise RateLimitError(TOOL_NAME)

    t0 = time.monotonic()
    try:
        result = await registry.open(dest.ntfy_topic, question, options, timeout_s)
    except (NtfyAskError, AuthError, TransportError, NtfyError) as e:
        payload = _error_payload(e)
        record: dict[str, Any] = {"tool": TOOL_NAME, "status": "error",
                                  "reason": payload["error"], "channel": dest.name,
                                  "transport": "ntfy"}
        if isinstance(e, TransportError | NtfyPostUnconfirmedError):
            record["error"] = (e if isinstance(e, TransportError) else e.transport).detail
        state.audit.write(record)
        return {**payload, "channel": dest.name}
    state.audit.write({
        "tool": TOOL_NAME, "status": "ok",
        "channel": dest.name, "transport": "ntfy",
        "question_id": result["question_id"],
        "source": result["source"],
        "ms": int((time.monotonic() - t0) * 1000),
    })
    result["channel"] = dest.name
    if columns != 1:
        result["note"] = "columns ignored on ntfy"
    return result


def _error_payload(e: Exception) -> dict[str, Any]:
    posted = isinstance(e, NtfyAskError) and e.posted
    where = ("question WAS posted, but no answer can be received" if posted
             else _NOT_POSTED)
    if isinstance(e, NtfyPostUnconfirmedError):
        return {"error": "transport_unreachable", "transport": e.transport.transport,
                "reason": e.transport.detail,
                "hint": "the question MAY have been posted (ntfy's response was lost); "
                        "mcgram marked it stopped if it was — check network connectivity"}
    if isinstance(e, NtfyTopicNotProtectedError):
        return {"error": "ntfy_topic_not_protected", "topic": e.topic,
                "reason": f"topic {e.topic!r} can be read without credentials, so anyone "
                          "could see the question or its answer buttons",
                "hint": f"deny anonymous read on the question topic and make the reply "
                        f"topic write-only for everyone ({_SETUP}); {where}"}
    if isinstance(e, NtfyReplyUnreachableError):
        return {"error": "ntfy_reply_unreachable",
                "reason": "could not subscribe to the ntfy reply topic",
                "hint": f"check the network and the ntfy server; {where}"}
    if isinstance(e, NtfyAuthFailedError | AuthError):
        return {"error": "ntfy_auth",
                "reason": "ntfy refused the access token (401/403)",
                "hint": f"check the token and that its user may read the reply topic and "
                        f"publish to the question topic; {where}"}
    if isinstance(e, TransportError):
        return {"error": "transport_unreachable", "transport": e.transport,
                "reason": e.detail,
                "hint": f"{_NOT_POSTED} — check network connectivity"}
    assert isinstance(e, NtfyError)
    return {"error": "ntfy_api", "reason": e.description, "hint": where}
