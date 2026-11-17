"""Operator inbox: `/commands` the operator types to the bot, stored for Claude to read.

The polling session filters each Telegram update much more strictly than the
poll loop does, then stores an accepted command as an HMAC-signed record in
`spool/<config-id>/inbox/`. Every session — polling or not — reads the records
meant for it through `check_inbox`. mcgram never acts on a command: it filters,
stores, expires and audits. See the operator-inbox section of
docs/security-threat-model.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeGuard

from .spool import Spool, heartbeat_status

if TYPE_CHECKING:
    from .audit import AuditLog
    from .rate_limiter import RateLimiter
    from .update_dispatcher import UpdateHandler

log = logging.getLogger("mcgram.inbox")

#: Bumped when the record or heartbeat shape changes. A reader that finds a
#: different protocol treats the records as absent and the poller as missing.
PROTOCOL_VERSION = 1
MAX_PENDING = 50
TEXT_MAX_CHARS = 1000
#: Telegram's `date` and our clock may disagree; a command sent this long
#: before a session started still counts as sent after it.
CLOCK_ALLOWANCE_S = 60
HEARTBEAT_KEY = "u0"
HEARTBEAT_KIND = "heartbeat"
RECORD_KIND = "cmd"
#: Telegram sends `/start` when a chat with the bot is first opened.
RESERVED_COMMANDS = frozenset({"start"})
#: Bucket name for intake in the shared `RateLimiter`.
INTAKE_BUCKET = "_inbox_intake"

_COMMAND_RE = re.compile(
    r"/([A-Za-z0-9_]{1,32})(?:@([A-Za-z0-9_]{1,32}))?(?:\s+(.*))?", re.DOTALL
)
_KEEP_CONTROL = frozenset("\n\t")
_FORWARD_FIELDS = ("forward_origin", "forward_date", "is_automatic_forward")

#: (status, reason) for an update that is not stored.
Refusal = tuple[str, str]


@dataclass(frozen=True)
class Command:
    """One accepted operator command, as parsed from a Telegram message."""

    update_id: int
    message_id: int
    chat_id: int
    command: str
    target: str | None
    args: str
    sent_at: int


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def sanitize(text: str) -> str:
    """Drop control and invisible format characters (Cc, Cf), keeping newline and tab.

    They render as nothing in a terminal or a prompt, so they could hide words
    from the operator's own reading of what Claude was told.
    """
    return "".join(
        ch for ch in text
        if ch in _KEEP_CONTROL or unicodedata.category(ch) not in ("Cc", "Cf")
    )


def parse_update(update: dict[str, Any], operator_chat_id: int) -> Command | Refusal | None:
    """A `Command`, a `(status, reason)` refusal to audit, or None to ignore silently.

    Checks run in a fixed order and the first failure wins. Updates without a
    message (button taps for `ask`) return None: auditing each would only add noise.
    """
    if "edited_message" in update:
        return "ignored", "edited"
    msg = update.get("message")
    if not isinstance(msg, dict):
        return None
    chat = _as_dict(msg.get("chat"))
    if chat.get("id") != operator_chat_id:
        return "rejected", "non_operator"
    if chat.get("type") != "private":
        return "rejected", "not_private"
    sender = _as_dict(msg.get("from"))
    if sender.get("id") != operator_chat_id or sender.get("is_bot") is not False \
            or "via_bot" in msg:
        return "rejected", "sender_mismatch"
    if any(field in msg for field in _FORWARD_FIELDS):
        return "rejected", "forwarded"
    text = msg.get("text")
    if not isinstance(text, str):
        return "ignored", "not_text"
    match = _COMMAND_RE.fullmatch(text)
    if match is None:
        return "ignored", "not_command"
    command = match.group(1).lower()
    if command in RESERVED_COMMANDS:
        return "ignored", "reserved"
    args = (match.group(3) or "").strip()
    if len(args) > TEXT_MAX_CHARS:
        return "dropped", "too_long"
    update_id, message_id, sent_at = update.get("update_id"), msg.get("message_id"), \
        msg.get("date")
    if _is_int(update_id) and _is_int(message_id) and _is_int(sent_at):
        target = match.group(2)
        return Command(
            update_id=update_id, message_id=message_id, chat_id=operator_chat_id,
            command=command, target=target.lower() if target else None,
            args=sanitize(args), sent_at=sent_at,
        )
    return "rejected", "malformed"


def signing_key(bot_token: str) -> bytes:
    """Per-bot HMAC key. Every reader already holds the token, so ownership can
    move between sessions without invalidating stored records."""
    return hmac.new(bot_token.encode("utf-8"), b"mcgram-inbox", hashlib.sha256).digest()


def _signature(key: bytes, record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "sig"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _key_for(update_id: int) -> str:
    return f"u{update_id}"


def _update_id(key: str) -> int:
    return int(key[1:])


class InboxStore:
    """Signed command records plus the polling session's heartbeat, in one spool namespace."""

    def __init__(
        self,
        spool: Spool,
        key: bytes,
        *,
        operator_chat_id: int,
        ttl_s: int,
        heartbeat_stale_s: int,
    ) -> None:
        self.spool = spool
        self._key = key
        self._operator_chat_id = operator_chat_id
        self._ttl_s = ttl_s
        self._heartbeat_stale_s = heartbeat_stale_s

    # --- intake (polling session only) -----------------------------------

    def publish(self, cmd: Command, now: float) -> str:
        """Store `cmd`. Returns "queued", "duplicate", or a drop reason.

        A record lives for `ttl_s` after it was received and its update must be
        younger than `ttl_s`, so a redelivery that arrives after the record was
        swept is already stale: sweep timing cannot reopen the dedupe window.
        """
        if now - cmd.sent_at > self._ttl_s:
            return "stale"
        key = _key_for(cmd.update_id)
        if len(self.keys()) >= MAX_PENDING:
            return "duplicate" if self._exists(key) else "inbox_full"
        record: dict[str, Any] = {
            "protocol": PROTOCOL_VERSION,
            "update_id": cmd.update_id,
            "message_id": cmd.message_id,
            "chat_id": cmd.chat_id,
            "command": cmd.command,
            "target": cmd.target,
            "args": cmd.args,
            "sent_at": cmd.sent_at,
            "received_at": now,
            "received_by_pid": os.getpid(),
            "expires_at": now + self._ttl_s,
        }
        record["sig"] = _signature(self._key, record)
        if self.spool.write_exclusive(key, RECORD_KIND, record):
            return "queued"
        return "duplicate" if self._exists(key) else "write_failed"

    def _exists(self, key: str) -> bool:
        path = self.spool.path_for(key, RECORD_KIND)
        return path is not None and path.exists()

    # --- readers (every session) -----------------------------------------

    def keys(self) -> set[str]:
        """Keys of stored commands. Mcgram-minted keys (`u_<hex>`) are valid spool
        keys too, but never name an update, so they are left out."""
        return {k for k in self.spool.keys(RECORD_KIND) if k[1:].isdigit()}

    def unread(
        self, seen: set[str], label: str, since: float, now: float
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """(commands for this session oldest first, keys whose signature failed).

        A file that does not parse is skipped without being marked: an
        `O_EXCL` record is written in place, so it can be read half-written.
        """
        found: list[dict[str, Any]] = []
        forged: list[str] = []
        for key in sorted(self.keys() - seen, key=_update_id):
            rec = self.spool.read(key, RECORD_KIND)
            if rec is None or rec.get("protocol") != PROTOCOL_VERSION:
                continue
            if not self._authentic(key, rec):
                forged.append(key)
                continue
            if self._visible(rec, label, since, now):
                found.append({"id": key, **rec})
        return found, forged

    def _authentic(self, key: str, rec: dict[str, Any]) -> bool:
        """Signed by this bot's key, and stored under its own update id — a valid
        record copied to a new name would otherwise read as a new command."""
        sig = rec.get("sig")
        if not isinstance(sig, str):
            return False
        try:
            body_ok = hmac.compare_digest(sig, _signature(self._key, rec))
        except (TypeError, ValueError, RecursionError):  # non-ASCII sig, odd body
            return False
        update_id = rec.get("update_id")
        return body_ok and _is_int(update_id) and _key_for(update_id) == key

    def _visible(self, rec: dict[str, Any], label: str, since: float, now: float) -> bool:
        sent_at, expires_at, target = rec.get("sent_at"), rec.get("expires_at"), \
            rec.get("target")
        return (
            _is_number(sent_at) and _is_number(expires_at)
            and rec.get("chat_id") == self._operator_chat_id
            and sent_at >= since - CLOCK_ALLOWANCE_S
            and expires_at >= now
            and (target is None or target == label)
        )

    # --- heartbeat ---------------------------------------------------------

    def beat(self) -> bool:
        now = time.time()
        return self.spool.write(HEARTBEAT_KEY, HEARTBEAT_KIND, {
            "pid": os.getpid(), "at": now, "protocol": PROTOCOL_VERSION,
            "expires_at": now + self._heartbeat_stale_s,
        })

    def withdraw(self) -> None:
        """Remove the heartbeat, but only one this process wrote."""
        hb = self.spool.read(HEARTBEAT_KEY, HEARTBEAT_KIND)
        if hb is not None and hb.get("pid") == os.getpid():
            self.spool.remove(HEARTBEAT_KEY, HEARTBEAT_KIND)

    def receiving(self) -> tuple[bool, str]:
        """(alive, reason) — is some session polling Telegram and storing commands?"""
        hb = self.spool.read(HEARTBEAT_KEY, HEARTBEAT_KIND)
        if hb is None:
            return False, "no session is polling Telegram for commands"
        return heartbeat_status(hb, protocol=PROTOCOL_VERSION,
                                stale_s=self._heartbeat_stale_s, what="inbox")


def make_intake(
    store: InboxStore, rate: RateLimiter, audit: AuditLog, operator_chat_id: int
) -> UpdateHandler:
    """The dispatcher handler that stores commands. Local file I/O only: it runs
    inside the poll loop, and a network call here would stall `getUpdates`."""

    async def intake(update: dict[str, Any]) -> None:
        parsed = parse_update(update, operator_chat_id)
        if parsed is None:
            return
        record: dict[str, Any] = {"tool": "_inbox", "update_id": update.get("update_id")}
        if not isinstance(parsed, Command):
            record["status"], record["reason"] = parsed
            audit.write(record)
            return
        record.update(message_id=parsed.message_id, command=parsed.command,
                      target=parsed.target, text=parsed.args)
        outcome = (store.publish(parsed, time.time())
                   if rate.try_acquire(INTAKE_BUCKET) else "rate_limited")
        if outcome == "duplicate":
            log.debug("inbox: update %s already stored", parsed.update_id)
            return
        if outcome == "queued":
            record["status"] = "queued"
        else:
            record["status"], record["reason"] = "dropped", outcome
            if outcome == "write_failed":
                log.warning("inbox: could not store update %s", parsed.update_id)
        audit.write(record)

    return intake
