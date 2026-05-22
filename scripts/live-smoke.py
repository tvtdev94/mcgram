"""Live smoke test — drives mcgram tool handlers against the real Telegram API.

Run with:
    MCGRAM_BOT_TOKEN=... MCGRAM_CONFIG=~/.mcgram/config.yaml uv run python scripts/live-smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

from mcgram.audit import AuditLog
from mcgram.config import Settings
from mcgram.rate_limiter import RateLimiter
from mcgram.reminders import ReminderScheduler
from mcgram.runtime import AppState
from mcgram.tg_client import TelegramClient
from mcgram.tools import cancel_reminder, list_reminders, send_file, send_message, set_reminder
from mcgram.update_dispatcher import UpdateDispatcher


async def main() -> int:
    cfg_path = os.environ.get("MCGRAM_CONFIG", str(Path("~/.mcgram/config.yaml").expanduser()))
    settings = Settings.load(cfg_path)
    token = settings.resolve_token()
    audit = AuditLog(settings.audit.path)
    rate = RateLimiter(settings.defaults.rate_limit_per_min)

    async with TelegramClient(token, api_root=settings.api_root) as client:
        state = AppState(
            settings=settings,
            client=client,
            dispatcher=UpdateDispatcher(),
            rate=rate,
            audit=audit,
        )
        state.reminders = ReminderScheduler(client, settings, audit)

        # 1. send_message
        print("→ send_message")
        r1 = await send_message.handle(
            state,
            text=f"🤖 mcgram live-smoke at {time.strftime('%H:%M:%S')}",
        )
        print("  result:", r1)

        # 2. send_file (this very script)
        print("→ send_file (live-smoke.py)")
        r2 = await send_file.handle(
            state,
            path=str(Path(__file__).resolve()),
            caption="live-smoke source",
        )
        print("  result:", r2)

        # 3. send_file too large → reject
        print("→ send_file too large (expect reject)")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin", dir=Path.cwd()) as f:
            big = Path(f.name)
        try:
            big.write_bytes(b"x" * 1024)
            state.settings.limits.file_max_bytes = 100  # tighten for the test
            r3 = await send_file.handle(state, path=str(big))
            print("  result:", r3)
        finally:
            big.unlink(missing_ok=True)
            state.settings.limits.file_max_bytes = 52_428_800  # restore

        # 4. set_reminder + list + cancel
        print("→ set_reminder(2s)")
        r4 = await set_reminder.handle(state, text="live-smoke reminder", delay_s=2)
        print("  result:", r4)
        rid = r4["reminder_id"]

        print("→ list_reminders")
        r5 = await list_reminders.handle(state)
        print("  result:", r5)

        # Wait for reminder to fire
        print("→ waiting 3s for reminder fire …")
        await asyncio.sleep(3)
        print("  reminder should have fired by now")

        # 5. set + immediately cancel
        print("→ set_reminder(60s) then cancel")
        r6 = await set_reminder.handle(state, text="cancel me", delay_s=60)
        rid2 = r6["reminder_id"]
        r7 = await cancel_reminder.handle(state, reminder_id=rid2)
        print("  cancel:", r7)

        print()
        print(f"audit at {settings.audit.path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
