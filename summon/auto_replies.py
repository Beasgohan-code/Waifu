"""Validation and exact matching for explicitly public group auto replies."""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Any

from summon.group_notes import (
    NOTE_NAME_PATTERN,
    NOTE_REVISION_PATTERN,
    validate_actor_id,
    validate_note_name,
    validate_revision,
)

MAX_AUTO_REPLIES = 20
MAX_TRIGGER_LENGTH = 64
MAX_TRIGGER_INPUT = 256
MAX_REPLY_LENGTH = 800
AUTO_REPLY_NAME_PATTERN = NOTE_NAME_PATTERN
AUTO_REPLY_REVISION_PATTERN = NOTE_REVISION_PATTERN
_FIELD = "auto_replies_v1"
_ENTRY_KEYS = frozenset(
    {
        "trigger",
        "response",
        "enabled",
        "revision",
        "created_at",
        "created_by",
        "updated_at",
        "updated_by",
    }
)


class AutoReplyConflict(RuntimeError):
    """The rule, revision or normalized trigger conflicts with the current state."""


class AutoReplyLimitError(AutoReplyConflict):
    """The group has reached the bounded rule capacity."""


class AutoReplyStorageError(RuntimeError):
    """Stored rules have an unexpected shape and must not be overwritten."""


def normalize_trigger(value: Any) -> str:
    """Normalize one short, single-line exact trigger, never a command or pattern."""
    if not isinstance(value, str) or len(value) > MAX_TRIGGER_INPUT:
        raise ValueError("Trigger must be a short text phrase")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise ValueError("Trigger contains unsupported control characters")
    # Normalize both before and after case folding, since folding can decompose.
    normalized = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", value).casefold())
    normalized = " ".join(normalized.split())
    if not 2 <= len(normalized) <= MAX_TRIGGER_LENGTH or normalized.startswith("/"):
        raise ValueError("Trigger must be 2–64 characters and cannot start with /")
    return normalized


def validate_reply_text(value: Any) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_REPLY_LENGTH:
        raise ValueError("Reply must contain 1–800 characters")
    # ZWNJ/ZWJ are required in some languages and emoji; block other invisible
    # format/control characters, while allowing ordinary multiline plain text.
    allowed = {"\n", "\t", "\u200c", "\u200d"}
    if any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs"} and char not in allowed for char in value
    ):
        raise ValueError("Reply contains unsupported control characters")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ValueError("Reply contains invalid Unicode") from exc


def validate_enabled(value: Any) -> None:
    if not isinstance(value, bool):
        raise ValueError("Enabled must be true or false")


def stored_rules(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate the entire map so damaged/ambiguous state cannot be overwritten."""
    value = document.get(_FIELD, {})
    if not isinstance(value, dict) or len(value) > MAX_AUTO_REPLIES:
        raise AutoReplyStorageError("Stored auto replies need operator review")
    try:
        triggers: set[str] = set()
        for name, rule in value.items():
            validate_note_name(name)
            if not isinstance(rule, dict) or set(rule) != _ENTRY_KEYS:
                raise ValueError("Invalid stored rule")
            trigger = normalize_trigger(rule.get("trigger"))
            if trigger != rule["trigger"] or trigger in triggers:
                raise ValueError("Invalid stored trigger")
            triggers.add(trigger)
            validate_reply_text(rule.get("response"))
            validate_enabled(rule.get("enabled"))
            validate_revision(rule.get("revision"))
            validate_actor_id(rule.get("created_by"))
            validate_actor_id(rule.get("updated_by"))
            if not isinstance(rule.get("created_at"), datetime) or not isinstance(
                rule.get("updated_at"), datetime
            ):
                raise ValueError("Invalid rule timestamps")
    except (ValueError, TypeError) as exc:
        # Neither untrusted names nor public text belong in logs or error messages.
        raise AutoReplyStorageError("Stored auto replies need operator review") from exc
    return value


def find_auto_reply(document: dict[str, Any], text: Any) -> str | None:
    """Return only public text for one exact match, never regex or substring matches."""
    rules = stored_rules(document)
    if not rules:
        return None
    try:
        trigger = normalize_trigger(text)
    except ValueError:
        return None
    for rule in rules.values():
        if rule["enabled"] and rule["trigger"] == trigger:
            return str(rule["response"])
    return None
