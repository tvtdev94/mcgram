"""Ask registry: open a question, await the operator's button tap, or time out."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .tg_client import TelegramClient

log = logging.getLogger("mcgram.ask")

#: Prefix marking a toggled-on option in a multi-select keyboard.
CHECK = "✅ "
#: callback_data suffix of the multi-select "Done" button (never a digit).
DONE = "done"
#: Upper bound on the chat edits made for a question abandoned at shutdown.
_ABANDON_EDIT_TIMEOUT_S = 5.0


@dataclass
class PendingAsk:
    question_id: str
    future: asyncio.Future
    message_id: int
    chat_id: int
    options: list[str]
    multi_select: bool = False
    columns: int = 1
    selected: set[int] = field(default_factory=set)
    client: Any = None  # for re-drawing the keyboard as options toggle


def _build_keyboard(
    question_id: str,
    options: list[str],
    *,
    columns: int = 1,
    multi_select: bool = False,
    selected: set[int] | None = None,
) -> dict[str, Any]:
    """`columns` buttons per row. callback_data encodes question_id + option index.

    Multi-select marks toggled options with a check and adds a final "Done" row.
    """
    chosen = selected or set()
    buttons = [
        {"text": (CHECK if idx in chosen else "") + opt, "callback_data": f"{question_id}:{idx}"}
        for idx, opt in enumerate(options)
    ]
    rows = [buttons[i:i + columns] for i in range(0, len(buttons), columns)]
    if multi_select:
        rows.append([{"text": "Done", "callback_data": f"{question_id}:{DONE}"}])
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
        options: list[str],
        timeout_s: int,
        multi_select: bool = False,
        columns: int = 1,
        deadline: float | None = None,
        on_outcome: Callable[[Any, str], None] | None = None,
    ) -> dict[str, Any]:
        """Post, wait, record the outcome in the chat.

        Answers come only from button taps: typed messages never resolve a
        question. With `multi_select` the result `value` is a list — the
        selected labels in option order, `[]` on timeout. Otherwise it is a
        string.

        `deadline` (epoch seconds) replaces "timeout_s after posting" for a
        caller whose clock started earlier: posting can sleep out a flood wait.
        `on_outcome(value, source)` runs as soon as the outcome is known, before
        the chat is edited — those edits can sleep out a flood wait too.
        """
        multi_select = bool(multi_select)
        q_id = "q_" + secrets.token_hex(4)
        reply_markup = _build_keyboard(q_id, options, columns=columns,
                                       multi_select=multi_select)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        # Registered before posting. getUpdates and sendMessage are separate
        # requests, so the operator's tap can reach the poll loop before
        # sendMessage's own response does; a tap can only come from this
        # message (its callback_data carries q_id), so accepting it early is
        # safe.
        pending = PendingAsk(
            question_id=q_id,
            future=fut,
            message_id=0,
            chat_id=chat_id,
            options=options,
            multi_select=multi_select,
            columns=columns,
            client=client,
        )
        self._pending[q_id] = pending
        try:
            msg = await client.send_message(chat_id, question, reply_markup=reply_markup)
            pending.message_id = msg["message_id"]
            wait_s = timeout_s if deadline is None else max(0.0, deadline - time.time())
            try:
                value, source = await asyncio.wait_for(fut, timeout=wait_s)
                if deadline is not None and time.time() > deadline:
                    # Tapped while the post was still returning, but by now the
                    # caller's clock has run out: it can no longer hear this.
                    raise TimeoutError
            except TimeoutError:
                empty: str | list[str] = [] if multi_select else ""
                if on_outcome is not None:
                    on_outcome(empty, "timeout")
                await _mark_timed_out(client, chat_id, msg["message_id"], question)
                return {"value": empty, "source": "timeout", "question_id": q_id}
            except asyncio.CancelledError:
                # Shutdown: nobody will read a tap on this question any more.
                await _mark_abandoned(client, chat_id, msg["message_id"], question)
                raise
        finally:
            self._pending.pop(q_id, None)
        if on_outcome is not None:
            on_outcome(value, source)
        await _mark_answered(client, chat_id, msg["message_id"], question, value)
        return {"value": value, "source": source, "question_id": q_id}

    async def handle_update(self, update: dict[str, Any]) -> None:
        # Typed messages are ignored on purpose: an answer is always a tap on
        # this question's own buttons, never a guess about which open question
        # a free-floating message belongs to.
        if cq := update.get("callback_query"):
            await self._handle_callback(cq)

    async def _handle_callback(self, cq: dict[str, Any]) -> None:
        data = cq.get("data", "")
        q_id, _, idx_str = data.partition(":")
        pending = self._pending.get(q_id)
        if pending and not pending.future.done():
            await _apply_tap(pending, idx_str)
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

    # Hook injected by server bootstrap so we can call answer_callback_query
    # without making the registry import the client (avoids cycle in runtime).
    _answer_hook: Any = None

    def set_answer_hook(self, hook: Any) -> None:
        self._answer_hook = hook


async def _apply_tap(pending: PendingAsk, idx_str: str) -> None:
    """Resolve on a single-select tap or on Done; toggle and redraw otherwise."""
    options = pending.options
    if pending.multi_select and idx_str == DONE:
        chosen = [opt for idx, opt in enumerate(options) if idx in pending.selected]
        pending.future.set_result((chosen, "button"))
        return
    if not idx_str.isdigit() or not 0 <= int(idx_str) < len(options):
        return
    idx = int(idx_str)
    if not pending.multi_select:
        pending.future.set_result((options[idx], "button"))
        return
    pending.selected ^= {idx}
    try:
        # Runs inside the poll loop: fail fast on a 429 rather than stall it.
        await pending.client.edit_message_reply_markup(
            pending.chat_id, pending.message_id,
            reply_markup=_build_keyboard(
                pending.question_id, options, columns=pending.columns,
                multi_select=True, selected=pending.selected,
            ),
            retry_flood_wait=False,
        )
    except Exception:
        # The selection is tracked here regardless; only the checkmarks lag.
        log.debug("redrawing multi-select keyboard failed (non-fatal)", exc_info=True)


async def _mark_answered(
    client: TelegramClient,
    chat_id: int,
    message_id: int,
    question: str,
    value: str | list[str],
) -> None:
    """Record the operator's answer in the chat by rewriting the question message.

    Without this the inline keyboard just vanishes on reply, leaving no trace in
    chat history of what was picked. Append the choice, then drop the buttons.
    """
    shown = (", ".join(value) or "—") if isinstance(value, list) else value
    try:
        await client.edit_message_text(
            chat_id, message_id, f"{question}\n\n✅ Đã chọn: {shown}"
        )
    except Exception:
        log.debug("edit_message_text on answer failed (non-fatal)", exc_info=True)
    await _strip_keyboard(client, chat_id, message_id)


async def _strip_keyboard(
    client: TelegramClient, chat_id: int, message_id: int, *, retry_flood_wait: bool = True
) -> None:
    try:
        await client.edit_message_reply_markup(chat_id, message_id, reply_markup=None,
                                               retry_flood_wait=retry_flood_wait)
    except Exception:
        log.debug("edit_message_reply_markup failed (non-fatal)", exc_info=True)


async def _mark_timed_out(
    client: TelegramClient, chat_id: int, message_id: int, question: str
) -> None:
    """Say so in the chat and drop the buttons, so nobody taps a dead question."""
    try:
        await client.edit_message_text(chat_id, message_id, f"{question}\n\n⏱ timed out")
    except Exception:
        log.debug("edit_message_text on timeout failed (non-fatal)", exc_info=True)
    await _strip_keyboard(client, chat_id, message_id)


async def _mark_abandoned(
    client: TelegramClient, chat_id: int, message_id: int, question: str
) -> None:
    """Say the question is dead and drop its buttons, on the way out.

    Runs while the process shuts down, so it is bounded and never sleeps out a
    flood wait: a stuck edit must not hold up the exit.
    """
    async def retire() -> None:
        with contextlib.suppress(Exception):
            await client.edit_message_text(
                chat_id, message_id, f"{question}\n\n⏹ mcgram stopped before an answer",
                retry_flood_wait=False,
            )
        await _strip_keyboard(client, chat_id, message_id, retry_flood_wait=False)

    try:
        await asyncio.wait_for(retire(), timeout=_ABANDON_EDIT_TIMEOUT_S)
    except TimeoutError:
        log.debug("retiring an abandoned question timed out (non-fatal)")
