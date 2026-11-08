"""Validated product-domain models shared by transports and services."""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

Rarity = Literal["common", "rare", "epic", "legendary"]
InventorySort = Literal["recent", "copies"]


def normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def player_level(xp: int) -> int:
    return 1 + math.isqrt(max(0, xp) // 100)


class CharacterInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=120)
    rarity: Rarity
    image_url: str = Field(max_length=2048)
    description: str = Field(default="", max_length=1200)
    active: bool = True

    @field_validator("image_url")
    @classmethod
    def valid_artwork(cls, value: str) -> str:
        if re.fullmatch(r"/art/[a-zA-Z0-9_-]+\.(?:png|jpe?g|webp)", value):
            return value
        parts = urlsplit(value)
        if parts.scheme == "https" and parts.hostname and not parts.username and not parts.password:
            return value
        raise ValueError("Artwork must use an HTTPS URL or an /art/ image path")


class GroupSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    spawn_enabled: bool = False
    spawn_every: int = Field(default=80, ge=20, le=10000)
    spawn_cooldown_seconds: int = Field(default=300, ge=60, le=86400)
    spawn_lifetime_seconds: int = Field(default=300, ge=60, le=1800)
    welcome_enabled: bool = False
    welcome_text: str = Field(default="Welcome to the group!", max_length=1000)
    goodbye_enabled: bool = False
    goodbye_text: str = Field(default="Thanks for being part of the community.", max_length=1000)
    rules: str = Field(default="", max_length=3000)
    link_filter: bool = False
    flood_enabled: bool = False
    flood_limit: int = Field(default=6, ge=3, le=30)
    flood_window_seconds: int = Field(default=10, ge=5, le=120)
    duplicate_enabled: bool = False
    duplicate_limit: int = Field(default=3, ge=2, le=10)
    mention_limit: int = Field(default=0, ge=0, le=20)
    blocked_keywords: list[str] = Field(default_factory=list, max_length=30)
    warning_limit: int = Field(default=3, ge=1, le=10)

    @field_validator("blocked_keywords")
    @classmethod
    def validate_keywords(cls, value: list[str]) -> list[str]:
        result = [normalize_name(word) for word in value]
        if any(not word or len(word) > 64 for word in result):
            raise ValueError("Keywords must contain 1–64 characters")
        return list(dict.fromkeys(result))


class GroupSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    spawn_enabled: bool | None = None
    spawn_every: int | None = Field(default=None, ge=20, le=10000)
    spawn_cooldown_seconds: int | None = Field(default=None, ge=60, le=86400)
    spawn_lifetime_seconds: int | None = Field(default=None, ge=60, le=1800)
    welcome_enabled: bool | None = None
    welcome_text: str | None = Field(default=None, max_length=1000)
    goodbye_enabled: bool | None = None
    goodbye_text: str | None = Field(default=None, max_length=1000)
    rules: str | None = Field(default=None, max_length=3000)
    link_filter: bool | None = None
    flood_enabled: bool | None = None
    flood_limit: int | None = Field(default=None, ge=3, le=30)
    flood_window_seconds: int | None = Field(default=None, ge=5, le=120)
    duplicate_enabled: bool | None = None
    duplicate_limit: int | None = Field(default=None, ge=2, le=10)
    mention_limit: int | None = Field(default=None, ge=0, le=20)
    blocked_keywords: list[str] | None = Field(default=None, max_length=30)
    warning_limit: int | None = Field(default=None, ge=1, le=10)

    @field_validator("blocked_keywords")
    @classmethod
    def validate_keywords(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else GroupSettings.validate_keywords(value)
