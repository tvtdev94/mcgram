"""`mcgram init` — scaffold ~/.mcgram/, install skill, register MCP with Claude Code."""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import httpx

from .skill_installer import install_skill

_WELCOME_TEXT = (
    "mcgram installed! If you can see this notification, the ntfy bridge works. "
    "Claude Code can now ping this device when long tasks finish, send logs, "
    "and (with Telegram) ask you yes/no questions."
)


def _bundled_config_yaml() -> str:
    data = resources.files("mcgram.data").joinpath("config.example.yaml")
    return data.read_text(encoding="utf-8")


def _generate_topic() -> str:
    """Return a random ntfy topic: `mcgram-<16-hex>` (64-bit entropy)."""
    return f"mcgram-{secrets.token_hex(8)}"


_ENV_TEMPLATE = (
    "# mcgram credentials — never commit.\n"
    "# Only needed if you enable Telegram transport (uncomment `bot:` in config.yaml).\n"
    "# Get a token from @BotFather on Telegram (/newbot).\n"
    "MCGRAM_BOT_TOKEN=\n"
    "# NTFY_TOKEN=  # only for ntfy paid/self-hosted with auth\n"
)


_NEXT_STEPS = """\

Next steps — choose ONE (or both):

  [A] ntfy.sh (fastest, ~30 seconds, no token required):
    1. Install the ntfy app: https://ntfy.sh/  (iOS / Android / Web)
    2. In the app, subscribe to topic:  {topic}
       (server: {server})
    3. Verify:  mcgram doctor
    4. Restart Claude Code -> /mcp -> mcgram appears -> done

  [B] Telegram (required for 2-way `ask` tool):
    1. Talk to @BotFather -> /newbot -> copy token
    2. Get your chat ID: /start the bot, then visit
       https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates  -> copy `chat.id`
    3. Edit {env}        -> set MCGRAM_BOT_TOKEN
    4. Edit {cfg}        -> uncomment `bot:` block, set operator_chat_id
    5. Verify:  mcgram doctor
"""


def _claude_cli_available() -> bool:
    return shutil.which("claude") is not None


def _is_already_registered() -> bool:
    """Return True if `mcgram` already in `claude mcp list`."""
    try:
        result = subprocess.run(
            ["claude", "mcp", "list"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False
    return any(
        line.strip().startswith("mcgram:") or line.strip().startswith("mcgram ")
        for line in (result.stdout or "").splitlines()
    )


def _register_with_claude_code(cfg: Path, *, force: bool) -> None:
    """Run `claude mcp add` so the user doesn't have to. No-ops if claude CLI absent."""
    if not _claude_cli_available():
        print("skip     `claude` CLI not found — run `claude mcp add` manually later")
        return

    already = _is_already_registered()
    if already and not force:
        print("skipped  mcgram already in `claude mcp list` (use --force to re-register)")
        return

    if already and force:
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            subprocess.run(
                ["claude", "mcp", "remove", "mcgram"],
                check=False, capture_output=True, timeout=10,
            )

    try:
        result = subprocess.run(
            [
                "claude", "mcp", "add",
                "--scope", "user", "mcgram",
                "--env", f"MCGRAM_CONFIG={cfg}",
                "--", "mcgram",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as e:
        print(f"skip     `claude mcp add` failed: {e}")
        return

    if result.returncode == 0:
        print("registered  mcgram with Claude Code (scope: user)")
    else:
        print(f"skip        `claude mcp add` exited {result.returncode}")
        if result.stderr:
            print(f"            stderr: {result.stderr.strip()[:200]}")


def _render_config_template(topic: str, *, tg_disable_polling: bool) -> str:
    """Substitute placeholders into the bundled config template: the generated
    ntfy topic and the detected Telegram reachability (`disable_polling`)."""
    return (
        _bundled_config_yaml()
        .replace("{{NTFY_TOPIC}}", topic)
        .replace("{{TG_DISABLE_POLLING}}", "true" if tg_disable_polling else "false")
    )


def _probe_telegram_reachable(
    api_root: str = "https://api.telegram.org", timeout: float = 5.0
) -> bool:
    """Best-effort: can this machine reach the Telegram Bot API?

    Any HTTP response (even 404) means the host is reachable -> True. A connect
    or timeout error means it is blocked/unreachable -> False. Never raises.

    Returns True (skips the probe) when MCGRAM_INIT_NO_TG_PROBE is set — for
    air-gapped installs and tests, where 'reachable' keeps disable_polling=false.
    """
    if os.environ.get("MCGRAM_INIT_NO_TG_PROBE", "").strip().lower() in {"1", "true", "yes"}:
        return True
    try:
        httpx.get(api_root, timeout=timeout)
        return True
    except httpx.HTTPError:
        return False


def _seed_ntfy_subscription(server: str, topic: str) -> None:
    """POST a welcome message so the topic exists server-side and the user's
    fresh subscription has something to display. Silent on network failure —
    init should never fail because ntfy is briefly unreachable.

    Set MCGRAM_INIT_NO_WELCOME=1 to skip (used by tests + air-gapped installs).
    """
    if os.environ.get("MCGRAM_INIT_NO_WELCOME", "").strip().lower() in {"1", "true", "yes"}:
        return
    url = f"{server.rstrip('/')}/{topic}"
    try:
        r = httpx.post(
            url,
            content=_WELCOME_TEXT.encode("utf-8"),
            headers={"Title": "mcgram welcome", "Tags": "wave,sparkles"},
            timeout=5.0,
        )
    except httpx.HTTPError as e:
        print(
            f"warning  could not seed ntfy topic ({type(e).__name__}); "
            "subscription will still work"
        )
        return
    if r.status_code == 200:
        print(f"seeded   welcome message published to {url}")
    else:
        print(
            f"warning  ntfy returned HTTP {r.status_code} when seeding topic — "
            "subscription will still work"
        )


def _read_existing_topic(cfg: Path) -> str | None:
    """Best-effort: pull ntfy.default_topic out of an already-scaffolded config.

    Used so the printed `Next steps` references the SAME topic that's actually
    in config when init is run idempotently.
    """
    try:
        import yaml as _yaml  # local import to keep startup fast on --help
        raw = _yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    ntfy = (raw.get("ntfy") or {}) if isinstance(raw, dict) else {}
    topic = ntfy.get("default_topic") if isinstance(ntfy, dict) else None
    return str(topic) if isinstance(topic, str) and topic else None


def init_config(*, force: bool = False) -> int:
    """Scaffold ~/.mcgram/, install skill, register MCP. Idempotent."""
    home = Path.home() / ".mcgram"
    home.mkdir(parents=True, exist_ok=True)
    cfg = home / "config.yaml"
    env = home / ".env"

    created: list[str] = []
    skipped: list[str] = []

    fresh_scaffold = False
    tg_reachable = True
    if not cfg.exists() or force:
        topic = _generate_topic()
        tg_reachable = _probe_telegram_reachable()
        cfg.write_text(
            _render_config_template(topic, tg_disable_polling=not tg_reachable),
            encoding="utf-8",
        )
        created.append(str(cfg))
        fresh_scaffold = True
    else:
        topic = _read_existing_topic(cfg) or "<not set in config>"
        skipped.append(str(cfg))

    if not env.exists() or force:
        env.write_text(_ENV_TEMPLATE, encoding="utf-8")
        created.append(str(env))
    else:
        skipped.append(str(env))

    for p in created:
        print(f"created  {p}")
    for p in skipped:
        print(f"skipped  {p} (already exists; use --force to overwrite)")

    if fresh_scaffold:
        if tg_reachable:
            print("detect   telegram reachable — enable `bot:` later for 2-way `ask`")
        else:
            print(
                "detect   api.telegram.org unreachable — this machine looks "
                "Telegram-blocked; ntfy stays default (bot.disable_polling pre-set true)"
            )

    install_skill(quiet=False, force=force)
    _register_with_claude_code(cfg, force=force)

    if fresh_scaffold and topic.startswith("mcgram-"):
        _seed_ntfy_subscription("https://ntfy.sh", topic)

    print(_NEXT_STEPS.format(
        env=env, cfg=cfg, topic=topic, server="https://ntfy.sh",
    ))
    return 0
