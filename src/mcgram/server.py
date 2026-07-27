"""MCP stdio server entry — registers tools, runs polling loop alongside MCP."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import __version__
from .audit import AuditLog
from .config import Settings
from .errors import ConfigError, MCGramError
from .ntfy_client import NtfyClient
from .poll_ownership import PollOwnership, cancel_task
from .polling import poll_loop
from .rate_limiter import RateLimiter
from .runtime import AppState
from .tg_client import TelegramClient
from .tools import send_file as tool_send_file
from .tools import send_message as tool_send_message
from .tools import send_video as tool_send_video
from .update_dispatcher import UpdateDispatcher

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("mcgram")

SERVER_NAME = "mcgram"


def _load_env_beside_config(settings_path: Path) -> None:
    env = settings_path.parent / ".env"
    if env.is_file():
        load_dotenv(env, override=False)


def _phase2_tool_modules() -> list:
    return [tool_send_message, tool_send_file, tool_send_video]


async def _serve(state: AppState) -> None:
    server: Server = Server(SERVER_NAME)
    modules = _phase2_tool_modules()
    if state.reminders is not None:
        # The tool list is fixed for the life of the process — MCP clients cache
        # it. `ask` is listed even when this instance doesn't own the Telegram
        # poll loop (it then returns a structured error) because ownership can
        # transfer at runtime, and a shrinking tool list would confuse clients.
        from .tools import ask, cancel_reminder, list_reminders, set_reminder
        modules += [ask, set_reminder, cancel_reminder, list_reminders]
    schemas = [Tool(**m.schema()) for m in modules]
    handlers = {m.TOOL_NAME: m.handle for m in modules}

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return schemas

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict | None) -> list[TextContent]:
        args = arguments or {}
        fn = handlers.get(name)
        if fn is None:
            payload = {"error": "unknown_tool", "name": name}
        else:
            try:
                payload = await fn(state, **args)
            except MCGramError as e:
                payload = {"error": type(e).__name__, "reason": str(e)}
            except TypeError as e:
                payload = {"error": "invalid_arguments", "reason": str(e)}
            except Exception as e:
                log.exception("tool %r failed", name)
                payload = {"error": "internal", "reason": f"{type(e).__name__}"}
        return [TextContent(type="text", text=json.dumps(payload, default=str, ensure_ascii=False))]

    async with stdio_server() as (reader, writer):
        await server.run(
            reader,
            writer,
            InitializationOptions(
                server_name=SERVER_NAME,
                server_version=__version__,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


async def _run() -> None:
    settings_path_str = _resolve_config_path_str()
    _load_env_beside_config(Path(settings_path_str))
    settings = Settings.load(settings_path_str)
    audit = AuditLog(
        settings.audit.path,
        rotate_mb=settings.audit.rotate_mb,
        timezone=settings.audit.timezone,
        redact_text=settings.audit.redact_text,
        retention_days=settings.audit.retention_days,
    )
    rate = RateLimiter(settings.defaults.rate_limit_per_min)
    dispatcher = UpdateDispatcher()
    async with _build_clients(settings) as (tg_client, ntfy_client):
        state = AppState(
            settings=settings,
            dispatcher=dispatcher,
            rate=rate,
            audit=audit,
            client=tg_client,
            ntfy_client=ntfy_client,
        )
        _wire_phase3(state)
        ownership, poll_task = _start_polling(state, settings)
        try:
            await _serve(state)
        finally:
            await cancel_task(poll_task)
            if ownership is not None:
                ownership.release()
            _shutdown_phase3(state)


def _start_polling(
    state: AppState, settings: Settings
) -> tuple[PollOwnership | None, asyncio.Task | None]:
    """Start the poll supervisor if this machine polls Telegram at all.

    Returns (ownership, task). Both are None when polling is off by
    configuration — no `bot` section, or `bot.disable_polling`. In that case
    `ask` is unavailable for a config reason, not a contention one, and no lock
    is taken: an ntfy-only instance must never block a Telegram instance from
    polling.
    """
    if state.client is None:
        log.info("no `bot` section in config — Telegram polling disabled (ntfy-only mode).")
        return None, None
    assert settings.bot is not None
    if settings.bot.disable_polling:
        log.info(
            "bot.disable_polling = true — Telegram polling skipped "
            "(send-only on this machine)."
        )
        return None, None

    ownership = PollOwnership(settings.lock_path, skip_lock=_env_bool("MCGRAM_SKIP_LOCK"))
    # Acquire before serving so the first `ask` sees a settled ownership state
    # rather than racing the supervisor's first loop iteration.
    ownership.attach(state)
    client, operator_chat_id = state.client, settings.bot.operator_chat_id
    task = asyncio.create_task(
        ownership.supervise(
            state,
            lambda: poll_loop(client, state.dispatcher, operator_chat_id, state.audit),
        ),
        name="mcgram-poll-supervisor",
    )
    return ownership, task


@contextlib.asynccontextmanager
async def _build_clients(settings: Settings):
    """Enter context for whichever transport clients are configured.

    When `bot:` is present but the token is unavailable, this is fatal UNLESS
    `bot.disable_polling = true` — that combo means "config is portable; this
    machine just can't reach Telegram", so we boot ntfy-only and log a warning.
    """
    tg_client: TelegramClient | None = None
    ntfy_client: NtfyClient | None = None
    async with contextlib.AsyncExitStack() as stack:
        if settings.bot is not None:
            try:
                token = settings.resolve_token()
            except ConfigError:
                if settings.bot.disable_polling:
                    log.warning(
                        "bot.disable_polling = true and %s is unset — "
                        "skipping Telegram client (send to telegram channels will fail).",
                        settings.bot.token_env,
                    )
                else:
                    raise
            else:
                tg_client = await stack.enter_async_context(
                    TelegramClient(token, api_root=settings.api_root)
                )
        if settings.ntfy is not None:
            ntfy_token = settings._resolve_ntfy_token()  # noqa: SLF001
            ntfy_client = await stack.enter_async_context(
                NtfyClient(settings.ntfy.server, access_token=ntfy_token)
            )
        yield tg_client, ntfy_client


def _wire_phase3(state: AppState) -> None:
    """Attach the reminder scheduler.

    The ask registry is deliberately NOT wired here: `ask` only works in the
    process that owns the Telegram poll loop, so `PollOwnership` attaches and
    detaches it as ownership changes. Reminders fire over one-way HTTP and are
    safe in every instance.
    """
    try:
        from .reminders import ReminderScheduler
    except ImportError:
        return
    state.reminders = ReminderScheduler(
        state.client, state.settings, state.audit, ntfy_client=state.ntfy_client,
    )


def _shutdown_phase3(state: AppState) -> None:
    if state.reminders is not None:
        state.reminders.shutdown()


def _resolve_config_path_str() -> str:
    import os
    return os.environ.get("MCGRAM_CONFIG", str(Path("~/.mcgram/config.yaml").expanduser()))


def _env_bool(name: str) -> bool:
    import os
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    # NOTE: LockHeldError is deliberately NOT handled here. A held lock means
    # another instance owns Telegram polling — that is a normal state (one
    # mcgram per Claude Code session), handled inside `PollOwnership` by
    # degrading to send-only. Exiting on it was the -32000 startup bug.
    from .errors import AuthError, ConfigError
    try:
        asyncio.run(_run())
    except ConfigError as e:
        sys.stderr.write(f"mcgram: config error: {e}\n")
        sys.exit(1)
    except AuthError as e:
        sys.stderr.write(f"mcgram: auth error: {e}\n")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
