"""Typed configuration loaded from YAML + environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .errors import ConfigError

DEFAULT_CONFIG_PATH = "~/.mcgram/config.yaml"
DEFAULT_HOME = "~/.mcgram"

ParseMode = Literal["plain", "markdown_v2"]


class BotConfig(BaseModel):
    token_env: str = "MCGRAM_BOT_TOKEN"
    operator_chat_id: int

    @field_validator("token_env")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("token_env must be non-empty")
        return v


class ChannelConfig(BaseModel):
    """A named destination (chat) the bot can reach.

    `default` is auto-populated from `bot.operator_chat_id` if not declared.
    """

    chat_id: int
    description: str | None = None


class DefaultsConfig(BaseModel):
    parse_mode: ParseMode = "plain"
    ask_timeout_s: int = Field(120, ge=1, le=600)
    rate_limit_per_min: int = Field(20, ge=1, le=600)


class LimitsConfig(BaseModel):
    ask_timeout_max_s: int = Field(600, ge=1)
    reminder_max_delay_s: int = Field(86400, ge=1)
    reminder_max_pending: int = Field(10, ge=1)
    file_max_bytes: int = Field(52_428_800, ge=1)
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


class Settings(BaseModel):
    bot: BotConfig
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
    def _seed_default_channel(self) -> Settings:
        """Ensure `default` channel always exists; backed by bot.operator_chat_id."""
        if DEFAULT_CHANNEL not in self.channels:
            self.channels[DEFAULT_CHANNEL] = ChannelConfig(
                chat_id=self.bot.operator_chat_id,
                description="Auto-created from bot.operator_chat_id",
            )
        return self

    def resolve_channel(self, name: str | None = None) -> int:
        """Return chat_id for the named channel. None → `default`."""
        key = name or DEFAULT_CHANNEL
        ch = self.channels.get(key)
        if ch is None:
            raise ConfigError(
                f"unknown channel {key!r}; known: {sorted(self.channels)}"
            )
        return ch.chat_id

    def channel_names(self) -> list[str]:
        return sorted(self.channels)

    def resolve_token(self) -> str:
        """Read bot token from the configured env var. Raises ConfigError if missing."""
        value = os.environ.get(self.bot.token_env)
        if not value:
            raise ConfigError(
                f"bot token env var {self.bot.token_env!r} is not set "
                f"(check ~/.mcgram/.env or MCGRAM_BOT_TOKEN)"
            )
        return value

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
