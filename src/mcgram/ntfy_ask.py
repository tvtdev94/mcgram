"""ask over ntfy: post a question with http action buttons, match the tap's token.

A tap makes the ntfy app POST `{"q": <question id>, "t": <option token>}` to the
reply topic, which anyone may publish to. So the button carries no credential;
the per-option random token is what authenticates an answer, and it is only
visible to readers of the read-protected question topic. The reply body never
leaves `_on_message`: the result is always one of the caller's own labels.

Every session subscribes on its own (ntfy fans each reply out to every
subscriber) and silently ignores question ids it did not open. No poll lock,
broker or spool is involved. See the ntfy section of
docs/security-threat-model.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio
import httpx

from .ask_registry import answered_text, stopped_text, timed_out_text
from .errors import MCGramError, TransportError
from .ntfy_reply_listener import AUDIT_TOOL, ReplyListener

if TYPE_CHECKING:
    from .audit import AuditLog
    from .ntfy_client import NtfyClient

log = logging.getLogger("mcgram.ntfy_ask")

#: ntfy allows at most three actions per notification.
MAX_OPTIONS = 3
#: Past this many bytes ntfy turns a message into an attachment.
MAX_MESSAGE_BYTES = 4096
#: Upper bound on waiting for the subscription to open before posting.
_READY_TIMEOUT_S = 10.0
#: Upper bound on retiring a question's notification, and on shutdown.
_RETIRE_TIMEOUT_S = 5.0
#: A real reply is about 60 bytes; anything far larger is not one.
_MAX_REPLY_BYTES = 1024
#: The replacement notification is silent: the outcome needs no alert.
_MARKER_PRIORITY = 2
#: The request reached the server but its response did not reach us, so ntfy
#: may have stored and delivered the question. A failed connect proves nothing
#: was sent.
_RESPONSE_LOST = (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError)


class NtfyAskError(MCGramError):
    """An ntfy ask failed. `posted` says whether the question reached the phone."""

    posted: bool = False


class NtfyTopicNotProtectedError(NtfyAskError):
    """A topic answered an anonymous read, so anyone could see or answer the question."""

    def __init__(self, topic: str) -> None:
        super().__init__(f"ntfy topic {topic!r} is readable without credentials")
        self.topic = topic


class NtfyReplyUnreachableError(NtfyAskError):
    """The reply topic could not be subscribed to, so no answer can arrive."""

    def __init__(self) -> None:
        super().__init__("ntfy reply topic unreachable")


class NtfyPostUnconfirmedError(NtfyAskError):
    """The question publish lost its response, so it may be on the phone."""

    def __init__(self, transport: TransportError) -> None:
        super().__init__(f"ntfy question publish unconfirmed: {transport.detail}")
        self.transport = transport


class NtfyAuthFailedError(NtfyAskError):
    """The reply-topic subscription was refused (401/403)."""

    def __init__(self) -> None:
        super().__init__("ntfy refused the reply-topic subscription (401/403)")


@dataclass
class _Pending:
    future: asyncio.Future[tuple[str, str]]
    options: list[str]
    tokens: list[str]
    task: asyncio.Task[Any] | None


class NtfyAsk:
    """Open questions for one process, and the reply listener while any are open."""

    def __init__(
        self, client: NtfyClient, *, server: str, reply_topic: str, audit: AuditLog
    ) -> None:
        self._client = client
        self._reply_url = f"{server.rstrip('/')}/{reply_topic}"
        self._reply_topic = reply_topic
        self._audit = audit
        self._pending: dict[str, _Pending] = {}
        self._listener = ReplyListener(
            client, reply_topic, on_message=self._on_message,
            on_fatal=self._on_fatal, audit=audit,
        )

    async def open(
        self, topic: str, question: str, options: list[str], timeout_s: float
    ) -> dict[str, Any]:
        """Post `question` to `topic` and wait for a tap or the deadline.

        Returns `{value, source: "button"|"timeout", question_id}`. Raises an
        `NtfyAskError` when the topics are unprotected, the reply topic cannot
        be reached, or ntfy refuses the subscription; client errors from the
        probe or the post propagate unchanged.

        Beyond `timeout_s` the call is bounded by: two probes and the post
        (each within the client's request timeout, normally well under a
        second), up to 10 s for the subscription to open, up to 5 s for the
        catch-up read, and up to 5 s for replacing the notification.
        """
        for checked in (topic, self._reply_topic):
            if not await self._client.anonymous_read_denied(checked):
                raise NtfyTopicNotProtectedError(checked)

        q_id = "q_" + secrets.token_hex(4)
        tokens = [secrets.token_urlsafe(16) for _ in options]
        future: asyncio.Future[tuple[str, str]] = asyncio.get_running_loop().create_future()
        self._pending[q_id] = _Pending(future, options, tokens, asyncio.current_task())
        # The question is published under its own id as sequence id, so its
        # notification can be replaced even when the publish response is lost.
        attempted = posted = False
        try:
            # Subscribe before posting, so even an instant tap is heard.
            self._listener.ensure_started()
            if not await self._listener.wait_ready(min(_READY_TIMEOUT_S, timeout_s)):
                if future.done():
                    future.result()  # the listener's own failure, e.g. auth
                raise NtfyReplyUnreachableError()
            attempted = True
            try:
                await self._client.publish(
                    topic, question, actions=self._actions(q_id, options, tokens),
                    sequence_id=q_id,
                )
            except TransportError as e:
                if isinstance(e.__cause__, _RESPONSE_LOST):
                    raise NtfyPostUnconfirmedError(e) from e
                raise
            posted = True
            value, source = await self._wait(future, timeout_s)
        except asyncio.CancelledError:
            # Shutdown: nobody will hear a tap on this question any more.
            if attempted:
                await self._retire(topic, q_id, _outcome(question, stopped_text))
            raise
        except NtfyAskError as e:
            e.posted = posted
            if attempted:
                await self._retire(topic, q_id, _outcome(question, stopped_text))
            raise
        finally:
            self._pending.pop(q_id, None)
            if not self._pending:
                with _shielded():
                    await self._listener.stop()
        text = (_outcome(question, lambda q: answered_text(q, value)) if source == "button"
                else _outcome(question, timed_out_text))
        await self._retire(topic, q_id, text)
        return {"value": value, "source": source, "question_id": q_id}

    async def shutdown(self) -> None:
        """Cancel every open ask (each retires its notification), then stop listening."""
        tasks = [p.task for p in self._pending.values()
                 if p.task is not None and not p.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=_RETIRE_TIMEOUT_S)
        await self._listener.stop()

    def _actions(
        self, q_id: str, options: list[str], tokens: list[str]
    ) -> list[dict[str, Any]]:
        return [
            {"action": "http", "label": label, "url": self._reply_url, "method": "POST",
             "body": json.dumps({"q": q_id, "t": token}), "clear": True}
            for label, token in zip(options, tokens, strict=True)
        ]

    async def _wait(
        self, future: asyncio.Future[tuple[str, str]], timeout_s: float
    ) -> tuple[str, str]:
        # `asyncio.wait`, not `wait_for`: on timeout the future must stay
        # uncancelled so the catch-up read below can still resolve it.
        await asyncio.wait({future}, timeout=timeout_s)
        if not future.done():
            was_connected = self._listener.connected
            caught_up = await self._listener.catch_up()
            if not future.done():
                if not caught_up and not was_connected:
                    # Taps may have been made but never reached us: saying
                    # "timeout" would read as "the operator ignored you".
                    raise NtfyReplyUnreachableError()
                return "", "timeout"
        return future.result()

    async def _retire(self, topic: str, sequence_id: str, text: str) -> None:
        """Replace the question's notification with its outcome (best effort).

        Servers from 2.16.0 and current apps update it in place by sequence id;
        elsewhere it shows as a new low-priority notification.
        """
        with _shielded():
            try:
                await self._client.publish(topic, text, priority=_MARKER_PRIORITY,
                                           sequence_id=sequence_id)
            except Exception:
                log.debug("retiring an ntfy question failed (non-fatal)", exc_info=True)

    def _on_message(self, event: dict[str, Any]) -> None:
        body = event.get("message")
        if not isinstance(body, str) or len(body.encode("utf-8")) > _MAX_REPLY_BYTES:
            return
        try:
            reply = json.loads(body)
        except ValueError:
            return
        if not isinstance(reply, dict):
            return
        q_id, token = reply.get("q"), reply.get("t")
        if not isinstance(q_id, str) or not isinstance(token, str):
            return
        pending = self._pending.get(q_id)
        if pending is None or pending.future.done():
            return  # another session's question, or a late or replayed tap
        given = token.encode("utf-8")
        matches = [i for i, expected in enumerate(pending.tokens)
                   if secrets.compare_digest(given, expected.encode("ascii"))]
        if not matches:
            # Never the token or the body: only that a known id was forged.
            self._audit.write({"tool": AUDIT_TOOL, "status": "rejected",
                               "reason": "bad_token", "question_id": q_id})
            return
        pending.future.set_result((pending.options[matches[0]], "button"))

    def _on_fatal(self, reason: str) -> None:
        log.warning("ntfy reply listener stopped: %s", reason)
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(NtfyAuthFailedError())


def _outcome(question: str, render: Callable[[str], str]) -> str:
    """`render(question)`, shortening the quoted question so the whole message
    fits in one ntfy message and can replace the notification."""
    text = render(question)
    if len(text.encode("utf-8")) <= MAX_MESSAGE_BYTES:
        return text
    budget = MAX_MESSAGE_BYTES - len(render("").encode("utf-8")) - len("…".encode())
    if budget <= 0:  # the outcome alone is too long (a huge label)
        return _clip(text, MAX_MESSAGE_BYTES)
    return render(_clip(question, budget) + "…")


def _clip(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def _shielded() -> anyio.CancelScope:
    """A scope for cleanup that must run even while the call is being cancelled.

    MCP cancels a call through an anyio cancel scope, which re-cancels every
    later await inside it, so an unshielded cleanup request is abandoned. The
    shield lets it finish; the deadline keeps shutdown bounded.
    """
    return anyio.CancelScope(shield=True, deadline=anyio.current_time() + _RETIRE_TIMEOUT_S)
