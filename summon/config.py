"""Environment-backed configuration; secrets are never bundled into the Mini App."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUMMON_", env_file=".env", extra="ignore")

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
                    bool(parsed.hostname) and parsed.username is None and parsed.password is None
                )
                secure = parsed.scheme == "https"
                local = parsed.scheme == "http" and parsed.hostname == "localhost"
                if (
                    not valid_host
                    or not (secure or local)
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
                    "Use exact HTTPS origins, or http://localhost in development"
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
                not parsed.hostname
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
            if self.webapp_url and urlsplit(self.webapp_url).scheme != "https":
                raise ValueError("SUMMON_WEBAPP_URL must use HTTPS in production")
            if any(urlsplit(origin).scheme != "https" for origin in self.allowed_origins):
                raise ValueError("Production origins must use HTTPS")
        if self.rich_enabled and (
            not self.telegram_api_id or not self.telegram_api_hash.get_secret_value()
        ):
            raise ValueError(
                "Outbound Wzgram requires SUMMON_TELEGRAM_API_ID and SUMMON_TELEGRAM_API_HASH"
            )
        return self
