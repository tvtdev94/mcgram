"""Operator inbox: `/commands` the operator types to the bot, stored for Claude to read.

The polling session filters each Telegram update much more strictly than the
poll loop does, then stores an accepted command as an HMAC-signed record in
`spool/<config-id>/inbox/`. Every session — polling or not — reads the records
meant for it through `check_inbox`. mcgram never acts on a command: it filters,
stores, expires and audits. See the operator-inbox section of
docs/security-threat-model.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeGuard

from .config import INBOX_TTL_MAX_S
from .spool import Spool, heartbeat_status

if TYPE_CHECKING:
    from .audit import AuditLog
    from .rate_limiter import RateLimiter
    from .tg_client import TelegramClient
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
#: The only commands stored. The skill defines what each one means to Claude.
ALLOWED_COMMANDS = frozenset({"status", "stop", "note"})
#: Bucket names in the shared `RateLimiter`.
INTAKE_BUCKET = "_inbox_intake"
DROP_BUCKET = "_inbox_drop"
#: Reaction put on an operator command that intake dropped. Must be on Telegram's
#: ReactionTypeEmoji list, which has no warning sign.
DROP_REACTION = "👎"
REACTION_TIMEOUT_S = 5.0

_COMMAND_RE = re.compile(
    r"/([A-Za-z0-9_]{1,32})(?:@([A-Za-z0-9_]{1,32}))?(?:\s+(.*))?", re.DOTALL
)
#: `/stop @api` — a target typed after a space would otherwise broadcast.
_SPACED_TARGET_RE = re.compile(r"@[A-Za-z0-9_]{1,32}(?:\s|$)")
_KEEP_CONTROL = frozenset("\n\t")
#: Control, format, and surrogate code points. A lone surrogate cannot be
#: encoded as UTF-8 at all, so it would break signing the record.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
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
    """Drop control, invisible format and lone surrogate characters, keeping newline and tab.

    The first two render as nothing in a terminal or a prompt, so they could hide
    words from the operator's own reading of what Claude was told.
    """
    return "".join(
        ch for ch in text
        if ch in _KEEP_CONTROL or unicodedata.category(ch) not in _DROPPED_CATEGORIES
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
    if command not in ALLOWED_COMMANDS:
        return "dropped", "unknown_command"
    target = match.group(2)
    args = (match.group(3) or "").strip()
    if target is None and _SPACED_TARGET_RE.match(args):
        return "dropped", "ambiguous_target"
    if len(args) > TEXT_MAX_CHARS:
        return "dropped", "too_long"
    update_id, message_id, sent_at = update.get("update_id"), msg.get("message_id"), \
        msg.get("date")
    if _is_int(update_id) and _is_int(message_id) and _is_int(sent_at):
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


def _fingerprint(rec: dict[str, Any]) -> str:
    """Identifies a file's content, so a changed forgery is audited again."""
    canonical = json.dumps(rec, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def sweep_records(spool: Spool, now: float, *, tmp_grace_s: int) -> int:
    """Expire records, and remove command files that claim to live longer than
    any `inbox.ttl_s` can make them — only a planted file does, and it would
    otherwise sit there until its claimed expiry."""
    removed = spool.sweep(now, tmp_grace_s=tmp_grace_s)
    horizon = now + INBOX_TTL_MAX_S + CLOCK_ALLOWANCE_S
    for key, rec in spool.list(RECORD_KIND):
        expires_at = rec.get("expires_at")
        if _is_number(expires_at) and expires_at > horizon and spool.remove(key, RECORD_KIND):
            removed += 1
    return removed


async def send_reaction(
    client: TelegramClient, chat_id: int, message_id: int, emoji: str
) -> None:
    """Best effort: never raises, never retries a flood wait, gives up after 5 s."""
    try:
        await asyncio.wait_for(
            client.set_message_reaction(chat_id, message_id, emoji), REACTION_TIMEOUT_S)
    except Exception as e:  # a reaction is only a signal; any failure is only logged
        log.info("inbox: %s reaction on message %s failed: %s", emoji, message_id, e)


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
        """Store `cmd`. Returns "queued", "replaced", "duplicate", or a drop reason.

        A record lives for `ttl_s` past the later of its Telegram date and its
        receipt, and an update must be younger than `ttl_s`, so a redelivery
        that arrives after the record was swept is already stale — even when
        our clock runs behind Telegram's: sweep timing cannot reopen dedupe.

        A file already under this update's name counts as a duplicate only when
        it is a genuine record. Anything else there — planted, tampered, or
        half-written — is replaced ("replaced"), so it cannot mute the command.
        """
        if now - cmd.sent_at > self._ttl_s:
            return "stale"
        key = _key_for(cmd.update_id)
        occupied = self._exists(key)
        if occupied and self._genuine(key) is not None:
            return "duplicate"
        if self._live_count(now) >= MAX_PENDING:
            return "inbox_full"
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
            "expires_at": max(now, cmd.sent_at) + self._ttl_s,
        }
        record["sig"] = _signature(self._key, record)
        if occupied:
            return "replaced" if self.spool.write(key, RECORD_KIND, record) else "write_failed"
        if self.spool.write_exclusive(key, RECORD_KIND, record):
            return "queued"
        # Lost a race with another poller, or the disk refused the write.
        return "duplicate" if self._genuine(key) is not None else "write_failed"

    def _exists(self, key: str) -> bool:
        path = self.spool.path_for(key, RECORD_KIND)
        return path is not None and path.exists()

    def _genuine(self, key: str) -> dict[str, Any] | None:
        rec = self.spool.read(key, RECORD_KIND)
        return rec if rec is not None and self._authentic(key, rec) else None

    def _live_count(self, now: float) -> int:
        """Signed, unexpired records, counted up to the cap. Junk files a local
        process plants, and records already past their expiry, never fill it."""
        live = 0
        for key in self.keys():
            rec = self._genuine(key)
            expires_at = rec.get("expires_at") if rec is not None else None
            if _is_number(expires_at) and expires_at >= now:
                live += 1
                if live >= MAX_PENDING:
                    break
        return live

    # --- readers (every session) -----------------------------------------

    def keys(self) -> set[str]:
        """Keys of stored commands. Mcgram-minted keys (`u_<hex>`) are valid spool
        keys too, but never name an update, so they are left out."""
        return {k for k in self.spool.keys(RECORD_KIND) if k[1:].isdigit()}

    def unread(
        self, seen: Collection[str], label: str, since: float, now: float
    ) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        """(commands for this session oldest first, (key, fingerprint) of forged files).

        A file that does not parse is skipped without being reported: an
        `O_EXCL` record is written in place, so it can be read half-written.
        A forged file is reported with a fingerprint of its content, because
        intake may later replace it with the genuine command of that name.
        """
        found: list[dict[str, Any]] = []
        forged: list[tuple[str, str]] = []
        for key in sorted(self.keys().difference(seen), key=_update_id):
            rec = self.spool.read(key, RECORD_KIND)
            if rec is None or rec.get("protocol") != PROTOCOL_VERSION:
                continue
            if not self._authentic(key, rec):
                forged.append((key, _fingerprint(rec)))
                continue
            if self._visible(rec, label, since, now):
                found.append({**rec, "id": key})
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
    store: InboxStore,
    rate: RateLimiter,
    audit: AuditLog,
    operator_chat_id: int,
    client: TelegramClient | None,
) -> UpdateHandler:
    """The dispatcher handler that stores commands.

    It runs inside the poll loop, so it does local file I/O only. The one
    network call — a 👎 on an operator command it had to drop, so a lost
    `/stop` is visible in the chat — runs in a background task.
    """
    senders: set[asyncio.Task[None]] = set()

    def signal_drop(message_id: Any) -> None:
        if client is None or not _is_int(message_id) or not rate.try_acquire(DROP_BUCKET):
            return
        task = asyncio.create_task(
            send_reaction(client, operator_chat_id, message_id, DROP_REACTION))
        senders.add(task)  # keep a reference until it finishes
        task.add_done_callback(senders.discard)

    async def intake(update: dict[str, Any]) -> None:
        parsed = parse_update(update, operator_chat_id)
        if parsed is None:
            return
        record: dict[str, Any] = {"tool": "_inbox", "update_id": update.get("update_id")}
        if not isinstance(parsed, Command):
            record["status"], record["reason"] = parsed
            audit.write(record)
            if record["status"] == "dropped":  # the operator's own command, refused
                signal_drop(_as_dict(update.get("message")).get("message_id"))
            return
        record.update(message_id=parsed.message_id, command=parsed.command,
                      target=parsed.target, text=parsed.args)
        outcome = (store.publish(parsed, time.time())
                   if rate.try_acquire(INTAKE_BUCKET) else "rate_limited")
        if outcome in ("queued", "replaced"):
            record["status"] = "queued"
            if outcome == "replaced":
                log.warning("inbox: replaced a forged file under update %s", parsed.update_id)
                record["replaced"] = "bad_signature"
        elif outcome == "duplicate":
            record["status"], record["reason"] = "ignored", "duplicate"
        else:
            record["status"], record["reason"] = "dropped", outcome
            if outcome == "write_failed":
                log.warning("inbox: could not store update %s", parsed.update_id)
            signal_drop(parsed.message_id)
        audit.write(record)

    return intake
