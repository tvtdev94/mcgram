"""Typed configuration loaded from YAML + environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .errors import ConfigError

DEFAULT_CONFIG_PATH = "~/.mcgram/config.yaml"
DEFAULT_HOME = "~/.mcgram"

ParseMode = Literal["plain", "markdown_v2"]
Transport = Literal["telegram", "ntfy", "discord"]


class BotConfig(BaseModel):
    token_env: str = "MCGRAM_BOT_TOKEN"
    operator_chat_id: int
    # When true, skip the Telegram long-poll loop in the MCP server. Use on
    # machines where api.telegram.org is blocked but you still want to keep
    # the `bot:` section (e.g. for portable configs / a shared YAML across
    # multiple hosts). send_message on telegram channels will still try to
    # send (and fail audibly) — only the inbound polling stops.
    disable_polling: bool = False

    @field_validator("token_env")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("token_env must be non-empty")
        return v


class NtfyConfig(BaseModel):
    server: str = "https://ntfy.sh"
    default_topic: str | None = None
    access_token_env: str | None = None

    @field_validator("server")
    @classmethod
    def _check_server(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("ntfy.server must start with http:// or https://")
        return v.rstrip("/")


class DiscordConfig(BaseModel):
    """Global settings shared by every Discord channel.

    Holds only the display identity — the webhook URLs (address + secret) live
    per-channel in env vars, never here.
    """

    username: str = "mcgram"
    avatar_url: str | None = None

    @field_validator("avatar_url")
    @classmethod
    def _check_avatar(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("discord.avatar_url must start with http:// or https://")
        return v


class ChannelConfig(BaseModel):
    """A named destination the bot can reach.

    Telegram channels need `chat_id`. ntfy channels need `ntfy_topic`
    (or fall back to `ntfy.default_topic` at resolve time). Discord channels
    need `discord_webhook_env` — the name of an env var holding the webhook URL.
    """

    transport: Transport = "telegram"
    chat_id: int | None = None
    ntfy_topic: str | None = None
    discord_webhook_env: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _validate_per_transport(self) -> ChannelConfig:
        if self.transport == "telegram" and self.chat_id is None:
            raise ValueError("telegram channel requires chat_id")
        if self.transport == "discord" and not self.discord_webhook_env:
            raise ValueError(
                "discord channel requires discord_webhook_env "
                "(env var name holding the webhook URL, set in ~/.mcgram/.env)"
            )
        return self


class DefaultsConfig(BaseModel):
    parse_mode: ParseMode = "plain"
    ask_timeout_s: int = Field(120, ge=1, le=600)
    rate_limit_per_min: int = Field(20, ge=1, le=600)


class LimitsConfig(BaseModel):
    ask_timeout_max_s: int = Field(600, ge=1)
    reminder_max_delay_s: int = Field(86400, ge=1)
    reminder_max_pending: int = Field(10, ge=1)
    # Hard upper bound (50 MB matches the Telegram bot API limit).
    file_max_bytes: int = Field(52_428_800, ge=1)
    # Effective cap for ntfy.sh transport (public free tier ~15 MB; self-hosters
    # can raise this in their config). Resolved as min(file_max_bytes, ntfy_file_max_bytes).
    ntfy_file_max_bytes: int = Field(15_728_640, ge=1)
    # Effective cap for Discord webhooks (default upload limit is 25 MB).
    # Resolved as min(file_max_bytes, discord_file_max_bytes).
    discord_file_max_bytes: int = Field(26_214_400, ge=1)
    ask_options_max: int = Field(6, ge=1, le=20)
    caption_max_chars: int = Field(1024, ge=1, le=1024)
    reminder_text_max_chars: int = Field(1000, ge=1)


class AuditConfig(BaseModel):
    path: str = "~/.mcgram/audit.jsonl"
    rotate_mb: int = Field(25, ge=1)
    redact_text: bool = False
    retention_days: int | None = None
    timezone: str = "UTC"

    @field_validator("path")
    @classmethod
    def _expand_path(cls, v: str) -> str:
        return str(Path(v).expanduser())

    @field_validator("retention_days")
    @classmethod
    def _check_retention(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("retention_days must be > 0 if set")
        return v


DEFAULT_CHANNEL = "default"


@dataclass(frozen=True)
class Destination:
    """Resolved channel target — what tools actually need to dispatch.

    Exactly one transport's fields are meaningful, depending on `transport`:
    telegram → chat_id; ntfy → ntfy_topic + ntfy_server; discord →
    discord_webhook_url (+ display identity).

    Note: `thread_id` is deliberately absent — it is a per-call runtime argument,
    not part of the config-derived destination. It travels through `dispatch.*`
    kwargs instead.
    """

    name: str
    transport: Transport
    chat_id: int | None = None
    ntfy_topic: str | None = None
    ntfy_server: str | None = None
    ntfy_access_token: str | None = None
    discord_webhook_url: str | None = None
    discord_username: str | None = None
    discord_avatar_url: str | None = None


class Settings(BaseModel):
    bot: BotConfig | None = None
    ntfy: NtfyConfig | None = None
    discord: DiscordConfig | None = None
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    channels: dict[str, ChannelConfig] = Field(default_factory=dict)
    allow_outside_cwd: bool = False
    api_root: str = "https://api.telegram.org"

    @model_validator(mode="after")
    def _check_api_root(self) -> Settings:
        if not self.api_root.startswith(("http://", "https://")):
            raise ValueError("api_root must start with http:// or https://")
        return self

    @model_validator(mode="after")
    def _require_at_least_one_transport(self) -> Settings:
        has_discord_channel = any(
            c.transport == "discord" for c in self.channels.values()
        )
        if self.bot is None and self.ntfy is None and not has_discord_channel:
            raise ValueError(
                "config must define a `bot` (Telegram), `ntfy` (ntfy.sh), "
                "or at least one Discord channel"
            )
        return self

    @model_validator(mode="after")
    def _seed_default_channel(self) -> Settings:
        """Auto-seed `default` channel based on which transport can actually serve.

        User's explicit `channels.default` always wins. Otherwise Telegram is the
        default only when it can fully operate (polling on → `ask` works). A machine
        that blocks Telegram sets `bot.disable_polling=true`; there, if ntfy is
        configured, the default routes to ntfy instead — so notifications land
        somewhere reachable rather than a transport that can't be polled.

        Discord is seeded as default only as a last resort — when it is the sole
        configured transport and there is exactly one Discord channel (so the pick
        is unambiguous). With multiple Discord channels no default is seeded, which
        makes an un-named call fail loudly with the list of valid names.
        """
        if DEFAULT_CHANNEL in self.channels:
            return self
        ntfy_usable = self.ntfy is not None and bool(self.ntfy.default_topic)
        if self.bot is not None and not self.bot.disable_polling:
            self.channels[DEFAULT_CHANNEL] = ChannelConfig(
                transport="telegram",
                chat_id=self.bot.operator_chat_id,
                description="Auto-created from bot.operator_chat_id",
            )
        elif ntfy_usable:
            assert self.ntfy is not None
            self.channels[DEFAULT_CHANNEL] = ChannelConfig(
                transport="ntfy",
                ntfy_topic=self.ntfy.default_topic,
                description="Auto-created from ntfy.default_topic",
            )
        elif self.bot is None:
            discord_keys = [
                k for k, c in self.channels.items() if c.transport == "discord"
            ]
            if len(discord_keys) == 1:
                src = self.channels[discord_keys[0]]
                self.channels[DEFAULT_CHANNEL] = ChannelConfig(
                    transport="discord",
                    discord_webhook_env=src.discord_webhook_env,
                    description="Auto-created from the sole Discord channel",
                )
        elif self.bot is not None:
            # bot present but polling disabled and no ntfy fallback — still seed a
            # default so send-only Telegram deployments have a target.
            self.channels[DEFAULT_CHANNEL] = ChannelConfig(
                transport="telegram",
                chat_id=self.bot.operator_chat_id,
                description="Auto-created from bot.operator_chat_id (send-only)",
            )
        return self

    def resolve_destination(self, name: str | None = None) -> Destination:
        """Return a `Destination` for the named channel. None → `default`."""
        key = name or DEFAULT_CHANNEL
        ch = self.channels.get(key)
        if ch is None:
            raise ConfigError(
                f"unknown channel {key!r}; known: {sorted(self.channels)}"
            )
        if ch.transport == "telegram":
            if ch.chat_id is None:  # defensive; model validator already enforces
                raise ConfigError(f"telegram channel {key!r} missing chat_id")
            return Destination(name=key, transport="telegram", chat_id=ch.chat_id)
        if ch.transport == "discord":
            env_name = ch.discord_webhook_env
            if not env_name:  # defensive; model validator already enforces
                raise ConfigError(
                    f"discord channel {key!r} missing discord_webhook_env"
                )
            url = os.environ.get(env_name)
            if not url:
                raise ConfigError(
                    f"discord channel {key!r}: env var {env_name!r} is not set "
                    f"(check ~/.mcgram/.env)"
                )
            dc = self.discord or DiscordConfig()
            return Destination(
                name=key,
                transport="discord",
                discord_webhook_url=url,
                discord_username=dc.username,
                discord_avatar_url=dc.avatar_url,
            )
        # ntfy: backfill topic from defaults; require ntfy section configured
        if self.ntfy is None:
            raise ConfigError(
                f"channel {key!r} uses ntfy transport but no `ntfy` section in config"
            )
        topic = ch.ntfy_topic or self.ntfy.default_topic
        if topic is None:
            raise ConfigError(
                f"ntfy channel {key!r} has no topic "
                f"(set channels.{key}.ntfy_topic or ntfy.default_topic)"
            )
        return Destination(
            name=key,
            transport="ntfy",
            ntfy_topic=topic,
            ntfy_server=self.ntfy.server,
            ntfy_access_token=self._resolve_ntfy_token(),
        )

    def resolve_channel(self, name: str | None = None) -> int:
        """Legacy: returns chat_id for telegram channels.

        Raises ConfigError if the channel is not telegram. Prefer
        `resolve_destination` in new code.
        """
        dest = self.resolve_destination(name)
        if dest.transport != "telegram" or dest.chat_id is None:
            raise ConfigError(
                f"channel {dest.name!r} is not a telegram channel; "
                f"use resolve_destination() for transport-aware code"
            )
        return dest.chat_id

    def channel_names(self) -> list[str]:
        return sorted(self.channels)

    def resolve_token(self) -> str:
        """Read bot token from the configured env var. Raises ConfigError if missing."""
        if self.bot is None:
            raise ConfigError("no `bot` section configured; cannot resolve token")
        value = os.environ.get(self.bot.token_env)
        if not value:
            raise ConfigError(
                f"bot token env var {self.bot.token_env!r} is not set "
                f"(check ~/.mcgram/.env or MCGRAM_BOT_TOKEN)"
            )
        return value

    def _resolve_ntfy_token(self) -> str | None:
        if self.ntfy is None or not self.ntfy.access_token_env:
            return None
        return os.environ.get(self.ntfy.access_token_env)

    @property
    def home_dir(self) -> Path:
        """Resolve home directory from audit path (defaults to ~/.mcgram)."""
        return Path(self.audit.path).expanduser().parent

    @property
    def lock_path(self) -> Path:
        return self.home_dir / ".lock"

    @classmethod
    def load(cls, path: str | Path | None = None) -> Settings:
        """Load settings from YAML (path arg or MCGRAM_CONFIG env or default)."""
        resolved = _resolve_config_path(path)
        if not resolved.is_file():
            raise ConfigError(f"config file not found: {resolved}")
        try:
            with open(resolved, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"invalid YAML in {resolved}: {e}") from e
        try:
            return cls(**raw)
        except Exception as e:
            raise ConfigError(f"invalid config {resolved}: {e}") from e


def _resolve_config_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    env = os.environ.get("MCGRAM_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path(DEFAULT_CONFIG_PATH).expanduser()
