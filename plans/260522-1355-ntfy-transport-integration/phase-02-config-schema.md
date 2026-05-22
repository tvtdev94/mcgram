# Phase 02 — Config schema mở rộng

**Priority:** P0 — blocks tools branching
**Status:** PENDING
**Depends on:** phase-01 (NtfyError)

## Overview

Mở rộng `Settings` để hỗ trợ:
- `NtfyConfig` (optional)
- `BotConfig` optional (cho phép ntfy-only setup)
- `ChannelConfig.transport` + ntfy fields
- API mới: `resolve_destination(name) -> Destination` thay cho `resolve_channel(name) -> int`

## Schema

```python
class NtfyConfig(BaseModel):
    server: str = "https://ntfy.sh"
    default_topic: str | None = None
    access_token_env: str | None = None  # optional bearer token env var

class BotConfig(BaseModel):              # đã có, giữ nguyên
    token_env: str = "MCGRAM_BOT_TOKEN"
    operator_chat_id: int

Transport = Literal["telegram", "ntfy"]

class ChannelConfig(BaseModel):
    transport: Transport = "telegram"
    # telegram fields
    chat_id: int | None = None
    # ntfy fields
    ntfy_topic: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _validate_per_transport(self) -> ChannelConfig:
        if self.transport == "telegram" and self.chat_id is None:
            raise ValueError("telegram channel requires chat_id")
        if self.transport == "ntfy" and self.ntfy_topic is None:
            # NB: cho phép None ở đây vì Settings._seed có thể fill từ ntfy.default_topic
            pass
        return self

@dataclass(frozen=True)
class Destination:
    name: str
    transport: Transport
    # populated based on transport
    chat_id: int | None = None
    ntfy_topic: str | None = None
    ntfy_server: str | None = None
    ntfy_access_token: str | None = None

class Settings(BaseModel):
    bot: BotConfig | None = None         # ← optional
    ntfy: NtfyConfig | None = None       # ← new
    # ...

    @model_validator(mode="after")
    def _require_at_least_one_transport(self) -> Settings:
        if self.bot is None and self.ntfy is None:
            raise ValueError("config must define either `bot` (Telegram) or `ntfy` section")
        return self

    @model_validator(mode="after")
    def _seed_default_channel(self) -> Settings:
        if DEFAULT_CHANNEL in self.channels:
            return self
        if self.ntfy and self.ntfy.default_topic:
            self.channels[DEFAULT_CHANNEL] = ChannelConfig(
                transport="ntfy",
                ntfy_topic=self.ntfy.default_topic,
                description="Auto-created from ntfy.default_topic",
            )
        elif self.bot:
            self.channels[DEFAULT_CHANNEL] = ChannelConfig(
                transport="telegram",
                chat_id=self.bot.operator_chat_id,
                description="Auto-created from bot.operator_chat_id",
            )
        return self

    def resolve_destination(self, name: str | None = None) -> Destination:
        key = name or DEFAULT_CHANNEL
        ch = self.channels.get(key)
        if ch is None:
            raise ConfigError(f"unknown channel {key!r}; known: {sorted(self.channels)}")
        if ch.transport == "telegram":
            return Destination(name=key, transport="telegram", chat_id=ch.chat_id)
        # ntfy: backfill from defaults
        topic = ch.ntfy_topic or (self.ntfy.default_topic if self.ntfy else None)
        if topic is None:
            raise ConfigError(f"ntfy channel {key!r} has no topic (set channels.{key}.ntfy_topic or ntfy.default_topic)")
        server = (self.ntfy.server if self.ntfy else "https://ntfy.sh")
        token = self._resolve_ntfy_token()
        return Destination(name=key, transport="ntfy", ntfy_topic=topic, ntfy_server=server, ntfy_access_token=token)

    def _resolve_ntfy_token(self) -> str | None:
        if not self.ntfy or not self.ntfy.access_token_env:
            return None
        return os.environ.get(self.ntfy.access_token_env)
```

## Backward compat

`resolve_channel(name) -> int` giữ nguyên signature, **thin wrapper** quanh `resolve_destination`:

```python
def resolve_channel(self, name: str | None = None) -> int:
    """DEPRECATED: returns chat_id (telegram only). Use resolve_destination for transport-aware code."""
    dest = self.resolve_destination(name)
    if dest.transport != "telegram":
        raise ConfigError(f"channel {dest.name!r} is not a telegram channel; use resolve_destination")
    assert dest.chat_id is not None
    return dest.chat_id
```

Existing tests dùng `resolve_channel` không cần đổi (vẫn pass cho telegram channels). Tools mới sẽ dùng `resolve_destination` trực tiếp.

## Files

- `src/mcgram/config.py` — modify
- `tests/unit/test_config_ntfy.py` — new
- `tests/unit/test_config.py` — review/update các test hiện có nếu cần (default channel seed logic)

## Tests

### New cases
- [ ] `test_ntfy_only_config_loads_without_bot_section`
- [ ] `test_telegram_only_config_still_works` (regression)
- [ ] `test_no_bot_no_ntfy_fails`
- [ ] `test_default_channel_seeds_from_ntfy_when_no_telegram`
- [ ] `test_default_channel_seeds_from_bot_when_no_ntfy`
- [ ] `test_default_channel_user_override_respected` (user khai báo `channels.default.transport: ntfy` ưu tiên)
- [ ] `test_ntfy_channel_missing_topic_falls_back_to_default_topic`
- [ ] `test_ntfy_channel_no_topic_anywhere_raises`
- [ ] `test_telegram_channel_missing_chat_id_raises`
- [ ] `test_resolve_destination_returns_telegram_destination`
- [ ] `test_resolve_destination_returns_ntfy_destination_with_server`
- [ ] `test_resolve_channel_legacy_raises_on_ntfy_channel`
- [ ] `test_ntfy_access_token_resolved_from_env`

## Acceptance

- All existing config tests pass (no regression)
- New cases above pass
- ruff clean
- config.py vẫn <200 LOC (split helper if needed)
