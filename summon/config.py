"""Environment-backed configuration; secrets are never bundled into the Mini App."""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Literal
from unicodedata import category
from urllib.parse import SplitResult, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource


def _contains_unsafe_url_characters(value: str) -> bool:
    # urlsplit silently strips several controls and leading spaces. Reject the
    # original value instead of validating a normalized URL but using the input.
    return len(value) > 2048 or any(
        char.isspace() or category(char) in {"Cc", "Cf"} or char == "\\" for char in value
    )


def _valid_url_host(host: str | None) -> bool:
    if not host or "%" in host:  # IPv6 zones and percent-encoded hosts are ambiguous.
        return False
    try:
        if ":" in host:
            return isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
        ascii_host = host.encode("idna").decode("ascii")
        if ascii_host.endswith("."):
            ascii_host = ascii_host[:-1]
        if not ascii_host or len(ascii_host) > 253:
            return False
        labels = ascii_host.split(".")
        if all(label.isdecimal() for label in labels):
            return isinstance(ipaddress.ip_address(ascii_host), ipaddress.IPv4Address)
        return all(re.fullmatch(r"(?!-)[a-zA-Z0-9-]{1,63}(?<!-)", label) for label in labels)
    except (UnicodeError, ValueError):
        return False


def _browser_origin(parsed: SplitResult) -> str:
    # Browser Origin headers serialize scheme/host in lowercase, punycode IDNs,
    # compress IPv6 addresses and omit the default port.
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{ipaddress.IPv6Address(host).compressed}]"
    else:
        host = host.encode("idna").decode("ascii")
    port = parsed.port
    default_port = 443 if parsed.scheme == "https" else 80
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme}://{host}{suffix}"


# Within one source, existing SUMMON_* names win, then aliases in this order.
# Process environment values take precedence over values from .env.
_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "environment": ("ENVIRONMENT",),
    "bot_token": ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
    "session_secret": ("SESSION_SECRET",),
    "mongo_uri": ("MONGO_URL", "MONGO_URI", "MONGODB_URI", "MONGODB_URL"),
    "mongo_database": ("MONGO_DB_NAME", "DB_NAME", "DATABASE_NAME"),
    "webapp_url": ("WEBAPP_URL", "WEB_APP_URL"),
    "telegram_api_id": ("API_ID", "TELEGRAM_API_ID"),
    "telegram_api_hash": ("API_HASH", "TELEGRAM_API_HASH"),
    "telegram_session_database": ("TELEGRAM_SESSION_DB",),
    "artwork_backend": ("ARTWORK_BACKEND",),
    "imgbb_key": ("IMGBB_KEY", "IMGBB_API_KEY"),
    "db_channel": ("DB_CHANNEL", "DB_CHANNEL_ID"),
    "webhooks_disabled_confirmed": ("WEBHOOKS_DISABLED_CONFIRMED",),
    "rich_enabled": ("RICH_ENABLED",),
    "premium_emoji_enabled": ("PREMIUM_EMOJI_ENABLED",),
    "operator_ids": ("OPERATOR_IDS", "OWNER_ID"),
    "allowed_origins": ("ALLOWED_ORIGINS",),
}


class _AliasedEnvironment(PydanticBaseSettingsSource):
    """Keep each source's precedence while normalizing aliases to field names."""

    def __init__(self, source: EnvSettingsSource) -> None:
        super().__init__(source.settings_cls)
        self.source = source

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self.source.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        values = self.source()
        for field, aliases in _ENV_ALIASES.items():
            # An explicitly empty prefixed value is still authoritative and will
            # fail production validation rather than silently falling back.
            if field in values:
                continue
            for alias in aliases:
                key = alias if self.source.case_sensitive else alias.lower()
                value = self.source.env_vars.get(key)
                if value is not None:
                    if field in {"operator_ids", "allowed_origins"}:
                        text = value.strip()
                        if not text:
                            values[field] = []
                        elif alias != "OWNER_ID" and text.startswith("["):
                            try:
                                values[field] = json.loads(text)
                            except ValueError:
                                raise ValueError(f"Invalid {alias} list") from None
                        elif alias == "OWNER_ID":
                            values[field] = [text]
                        else:
                            # Simple comma-separated lists are accepted in
                            # addition to the existing SUMMON_* JSON lists.
                            values[field] = [item.strip() for item in text.split(",")]
                    else:
                        values[field] = value
                    break
        return values


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SUMMON_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        if not isinstance(env_settings, EnvSettingsSource) or not isinstance(
            dotenv_settings, EnvSettingsSource
        ):
            raise RuntimeError("Unsupported environment source")
        return (
            init_settings,
            _AliasedEnvironment(env_settings),
            _AliasedEnvironment(dotenv_settings),
            file_secret_settings,
        )

    environment: Literal["development", "production"] = "development"
    bot_token: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    mongo_uri: SecretStr = SecretStr("")
    mongo_database: str = Field(default="summon", pattern=r"^[A-Za-z0-9_-]{1,63}$")
    webapp_url: str = ""
    telegram_api_id: int | None = Field(default=None, gt=0, lt=2**31)
    telegram_api_hash: SecretStr = SecretStr("")
    telegram_session_database: str = Field(
        default="summon_telegram", pattern=r"^[A-Za-z0-9_-]{1,63}$"
    )
    artwork_backend: Literal["mongo", "imgbb"] = "mongo"
    imgbb_key: SecretStr = SecretStr("")
    db_channel: int | None = Field(default=None, lt=0, gt=-(2**63))
    webhooks_disabled_confirmed: bool = False
    rich_enabled: bool = True
    bot_api_rich_enabled: bool = True
    premium_emoji_enabled: bool = True
    allowed_origins: list[str] = []
    operator_ids: list[int] = []
    preview_mode: bool = False
    session_max_age: int = Field(default=3600, ge=60, le=86400)
    init_data_max_age: int = Field(default=3600, ge=60, le=86400)
    mongo_max_pool_size: int = Field(default=50, ge=1, le=500)
    poll_watchdog_seconds: int = Field(default=300, ge=120, le=3600)
    poller_heartbeat_enabled: bool = False
    poller_monitor_token: SecretStr = SecretStr("")
    poller_status_max_age_seconds: int = Field(default=120, ge=60, le=900)
    web_dist: str = ""

    @field_validator("telegram_api_id", "db_channel", mode="before")
    @classmethod
    def empty_optional_id(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("operator_ids")
    @classmethod
    def valid_operator_ids(cls, value: list[int]) -> list[int]:
        if any(not 0 < user_id < 2**53 for user_id in value):
            raise ValueError("Operator IDs must be positive Telegram user IDs")
        return list(dict.fromkeys(value))

    @field_validator("allowed_origins")
    @classmethod
    def no_wildcard_origins(cls, value: list[str]) -> list[str]:
        for origin in value:
            try:
                parsed = urlsplit(origin)
                port = parsed.port  # Force validation of malformed ports.
                valid_host = (
                    _valid_url_host(parsed.hostname)
                    and parsed.username is None
                    and parsed.password is None
                )
                secure = parsed.scheme == "https"
                local = parsed.scheme == "http" and parsed.hostname == "localhost"
                if (
                    not valid_host
                    or _contains_unsafe_url_characters(origin)
                    or not (secure or local)
                    or origin != _browser_origin(parsed)
                    or parsed.path
                    or parsed.query
                    or parsed.fragment
                    or "?" in origin
                    or "#" in origin
                    or (port is not None and port < 1)
                ):
                    raise ValueError
            except ValueError as exc:
                raise ValueError(
                    "Use browser-serialized HTTPS origins, or http://localhost in development"
                ) from exc
        return value

    @field_validator("webapp_url")
    @classmethod
    def valid_webapp_url(cls, value: str) -> str:
        if not value:
            return value
        try:
            parsed = urlsplit(value)
            port = parsed.port
            if (
                not _valid_url_host(parsed.hostname)
                or _contains_unsafe_url_characters(value)
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or "#" in value
                or parsed.query
                or "?" in value
                or parsed.scheme not in {"http", "https"}
                or (parsed.scheme == "http" and parsed.hostname != "localhost")
                or (port is not None and port < 1)
            ):
                raise ValueError
        except ValueError as exc:
            raise ValueError(
                "Web App URL must be an HTTPS URL without credentials, query or fragment"
            ) from exc
        return value

    @model_validator(mode="after")
    def validate_security(self) -> Settings:
        if self.artwork_backend == "imgbb" and not self.imgbb_key.get_secret_value().strip():
            raise ValueError("IMGBB_KEY is required when ARTWORK_BACKEND is imgbb")
        if self.environment == "production":
            if self.preview_mode:
                raise ValueError("Preview mode is not permitted in production")
            if not self.mongo_uri.get_secret_value():
                raise ValueError("SUMMON_MONGO_URI is required in production")
            if not self.bot_token.get_secret_value():
                raise ValueError("SUMMON_BOT_TOKEN is required in production")
            if len(self.session_secret.get_secret_value()) < 32:
                raise ValueError("SUMMON_SESSION_SECRET must be at least 32 characters")
            if not self.webapp_url or urlsplit(self.webapp_url).scheme != "https":
                raise ValueError("SUMMON_WEBAPP_URL must be a non-empty HTTPS URL in production")
            if any(urlsplit(origin).scheme != "https" for origin in self.allowed_origins):
                raise ValueError("Production origins must use HTTPS")
        if self.poller_heartbeat_enabled and (
            not self.bot_token.get_secret_value()
            or len(self.session_secret.get_secret_value()) < 32
        ):
            raise ValueError(
                "Polling health requires a bot token and a 32-character session secret"
            )
        if self.telegram_session_database == self.mongo_database:
            raise ValueError("Wzgram sessions require a separate MongoDB database")
        if self.environment == "production" and (
            not self.telegram_api_id or not self.telegram_api_hash.get_secret_value()
        ):
            raise ValueError("Wzgram requires SUMMON_TELEGRAM_API_ID and SUMMON_TELEGRAM_API_HASH")
        return self
