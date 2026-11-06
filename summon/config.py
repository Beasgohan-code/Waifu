"""Environment-backed configuration; secrets are never bundled into the Mini App."""

from __future__ import annotations

import ipaddress
import re
from typing import Literal
from unicodedata import category
from urllib.parse import SplitResult, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SUMMON_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    environment: Literal["development", "production"] = "development"
    bot_token: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    mongo_uri: SecretStr = SecretStr("")
    mongo_database: str = Field(default="summon", pattern=r"^[A-Za-z0-9_-]{1,63}$")
    webapp_url: str = ""
    telegram_api_id: int | None = None
    telegram_api_hash: SecretStr = SecretStr("")
    rich_enabled: bool = False
    bot_api_rich_enabled: bool = True
    premium_emoji_enabled: bool = True
    allowed_origins: list[str] = []
    operator_ids: list[int] = []
    preview_mode: bool = False
    session_max_age: int = Field(default=3600, ge=60, le=86400)
    init_data_max_age: int = Field(default=3600, ge=60, le=86400)
    mongo_max_pool_size: int = Field(default=50, ge=1, le=500)
    poll_watchdog_seconds: int = Field(default=300, ge=120, le=3600)
    web_dist: str = ""

    @field_validator("telegram_api_id", mode="before")
    @classmethod
    def empty_api_id(cls, value: object) -> object:
        return None if value == "" else value

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
        if self.rich_enabled and (
            not self.telegram_api_id or not self.telegram_api_hash.get_secret_value()
        ):
            raise ValueError(
                "Outbound Wzgram requires SUMMON_TELEGRAM_API_ID and SUMMON_TELEGRAM_API_HASH"
            )
        return self
