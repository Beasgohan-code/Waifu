"""Shared validation and limits for explicitly public group notes."""

from __future__ import annotations

import re
from typing import Any

MAX_NOTES = 30
MAX_NOTE_LENGTH = 1200
NOTE_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"
NOTE_REVISION_PATTERN = r"^[a-f0-9]{32}$"


class NoteConflict(RuntimeError):
    """The supplied revision no longer describes this note."""


class NoteLimitError(NoteConflict):
    """The group has reached its bounded note capacity."""


class NoteStorageError(RuntimeError):
    """Existing note data has an unexpected shape; do not overwrite it."""


def validate_group_id(group_id: Any) -> None:
    if isinstance(group_id, bool) or not isinstance(group_id, int) or not -(2**53) < group_id < 0:
        raise ValueError("A valid Telegram group is required")


def validate_actor_id(actor_id: Any) -> None:
    if isinstance(actor_id, bool) or not isinstance(actor_id, int) or not 0 < actor_id < 2**53:
        raise ValueError("A valid Telegram user is required")


def validate_note_name(name: Any) -> None:
    if not isinstance(name, str) or re.fullmatch(NOTE_NAME_PATTERN, name) is None:
        raise ValueError("Names use 1–32 lowercase letters, numbers, underscores or hyphens")


def validate_note_content(content: Any) -> None:
    if not isinstance(content, str) or not content.strip() or len(content) > MAX_NOTE_LENGTH:
        raise ValueError("Note text must contain 1–1200 characters")
    if any(ord(char) < 32 and char not in "\n\t" for char in content):
        raise ValueError("Note text contains unsupported control characters")
    try:
        content.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ValueError("Note text contains invalid Unicode") from exc


def validate_revision(revision: Any, *, allow_none: bool = False) -> None:
    if revision is None and allow_none:
        return
    if not isinstance(revision, str) or re.fullmatch(NOTE_REVISION_PATTERN, revision) is None:
        raise ValueError("A valid note revision is required")
