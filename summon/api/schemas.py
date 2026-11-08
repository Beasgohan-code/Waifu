"""Validation schemas and reusable HTTP path constraints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator

from summon.auto_replies import (
    MAX_REPLY_LENGTH,
    MAX_TRIGGER_INPUT,
    normalize_trigger,
    validate_reply_text,
)
from summon.group_notes import (
    MAX_NOTE_LENGTH,
    NOTE_NAME_PATTERN,
    NOTE_REVISION_PATTERN,
    validate_note_content,
)
from summon.models import CharacterInput

CharacterID = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]

TradeID = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")]

WarningID = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")]

WarningGroupID = Annotated[int, Path(ge=-(2**53 - 1), lt=0)]

WarningTargetID = Annotated[int, Path(gt=0, le=2**53 - 1)]

NoteName = Annotated[str, Path(min_length=1, max_length=32, pattern=NOTE_NAME_PATTERN)]


class CatalogReviewCommit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    character: CharacterInput
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class Toggle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: bool


class TradeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receiver_id: int = Field(gt=0, le=2**53 - 1)
    offer_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    request_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")


class GiftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receiver_id: int = Field(gt=0, le=2**53 - 1)
    character_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")


class ModerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: int = Field(gt=0, le=2**53 - 1)
    action: Literal["ban", "unban", "mute", "unmute", "kick", "warn"]
    reason: str = Field(default="", max_length=500)
    duration_seconds: int | None = Field(default=None, ge=60, le=366 * 86400)


class WarningRevocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=500)


class NoteWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=MAX_NOTE_LENGTH)
    # Explicit null creates only; an existing revision updates only that version.
    expected_revision: str | None = Field(pattern=NOTE_REVISION_PATTERN)

    @field_validator("content")
    @classmethod
    def valid_content(cls, value: str) -> str:
        validate_note_content(value)
        return value


class NoteDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str = Field(pattern=NOTE_REVISION_PATTERN)


class AutoReplyWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger: str = Field(min_length=1, max_length=MAX_TRIGGER_INPUT)
    response: str = Field(min_length=1, max_length=MAX_REPLY_LENGTH)
    enabled: bool = Field(strict=True)
    # Explicit null creates only; a revision updates only that exact version.
    expected_revision: str | None = Field(pattern=NOTE_REVISION_PATTERN)

    @field_validator("trigger")
    @classmethod
    def valid_trigger(cls, value: str) -> str:
        return normalize_trigger(value)

    @field_validator("response")
    @classmethod
    def valid_response(cls, value: str) -> str:
        validate_reply_text(value)
        return value
