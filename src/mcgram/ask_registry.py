"""Ask registry: open a question, await operator reply (button or freetext), or time out."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .tg_client import TelegramClient

log = logging.getLogger("mcgram.ask")


@dataclass
class PendingAsk:
    question_id: str
    future: asyncio.Future
    message_id: int
    chat_id: int
    options: list[str] | None
    posted_at: float  # epoch seconds; only replies posted AFTER this resolve the ask


def _build_keyboard(question_id: str, options: list[str]) -> dict[str, Any]:
    """One button per row. callback_data encodes question_id + option index."""
    rows = [
        [{"text": opt, "callback_data": f"{question_id}:{idx}"}]
        for idx, opt in enumerate(options)
    ]
    return {"inline_keyboard": rows}


class AskRegistry:
    """In-process map of open questions; handler resolves each on operator reply or timeout."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingAsk] = {}

    @property
    def open_count(self) -> int:
        return len(self._pending)

    async def open(
        self,
        client: TelegramClient,
        *,
        chat_id: int,
        question: str,
        options: list[str] | None,
        timeout_s: int,
    ) -> dict[str, Any]:
        q_id = "q_" + secrets.token_hex(4)
        reply_markup = _build_keyboard(q_id, options) if options else None
        msg = await client.send_message(chat_id, question, reply_markup=reply_markup)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[q_id] = PendingAsk(
            question_id=q_id,
            future=fut,
            message_id=msg["message_id"],
            chat_id=chat_id,
            options=options,
            posted_at=time.time(),
        )
        try:
            value, source = await asyncio.wait_for(fut, timeout=timeout_s)
            await _strip_keyboard(client, chat_id, msg["message_id"])
            return {"value": value, "source": source, "question_id": q_id}
        except TimeoutError:
            await _mark_timed_out(client, chat_id, msg["message_id"], question)
            return {"value": "", "source": "timeout", "question_id": q_id}
        finally:
            self._pending.pop(q_id, None)

    async def handle_update(self, update: dict[str, Any]) -> None:
        if cq := update.get("callback_query"):
            await self._handle_callback(cq)
            return
        if msg := update.get("message"):
            self._handle_message(msg)

    async def _handle_callback(self, cq: dict[str, Any]) -> None:
        data = cq.get("data", "")
        q_id, _, idx_str = data.partition(":")
        pending = self._pending.get(q_id)
        if pending and pending.options and idx_str.isdigit():
            idx = int(idx_str)
            if 0 <= idx < len(pending.options) and not pending.future.done():
                pending.future.set_result((pending.options[idx], "button"))
        # Always answer the callback so Telegram stops showing a spinner.
        # The client reference travels via the registry's known client (passed at open()).
        # Since handle_update lives on AppState, we look up the client via cq's bot wiring:
        # in mcgram, the dispatcher passes the raw update; we don't have client here.
        # The runtime injects an `_answer` hook below.
        if self._answer_hook is not None:
            try:
                await self._answer_hook(cq["id"])
            except Exception:
                log.exception("answer_callback_query failed")

    def _handle_message(self, msg: dict[str, Any]) -> None:
        chat_id = msg.get("chat", {}).get("id")
        text = msg.get("text") or ""
        msg_ts = float(msg.get("date") or 0)
        # Resolve the oldest pending ask in the same chat posted before this message.
        candidates = sorted(
            (p for p in self._pending.values() if p.chat_id == chat_id and msg_ts > p.posted_at),
            key=lambda p: p.posted_at,
        )
        for p in candidates:
            if not p.future.done():
                p.future.set_result((text, "freetext"))
                break

    # Hook injected by server bootstrap so we can call answer_callback_query
    # without making the registry import the client (avoids cycle in runtime).
    _answer_hook: Any = None

    def set_answer_hook(self, hook: Any) -> None:
        self._answer_hook = hook


async def _strip_keyboard(client: TelegramClient, chat_id: int, message_id: int) -> None:
    try:
        await client.edit_message_reply_markup(chat_id, message_id, reply_markup=None)
    except Exception:
        log.debug("edit_message_reply_markup failed (non-fatal)", exc_info=True)


async def _mark_timed_out(
    client: TelegramClient, chat_id: int, message_id: int, question: str
) -> None:
    try:
        await client.edit_message_text(chat_id, message_id, f"{question}\n\n(timed out)")
    except Exception:
        log.debug("edit_message_text on timeout failed (non-fatal)", exc_info=True)
