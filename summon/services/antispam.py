"""Bounded, group-configurable spam signals; never moderate an admin automatically."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from pymongo import ReturnDocument

from summon.models import GroupSettings, normalize_name

_LINK = re.compile(r"(?:https?://|www\.|(?:^|\s)t(?:elegram)?\.me/)", re.IGNORECASE)
_MENTION = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{5,32}\b")


def static_spam_reason(
    text: str, settings: GroupSettings, *, entities: Iterable[Any] = ()
) -> str | None:
    # Telegram text_link/text_mention entities can conceal a URL or user behind
    # innocent display text, so inspecting plain text alone is insufficient.
    typed_entities = []
    for entity in entities:
        entity_type = getattr(entity, "type", "")
        # Wzgram enum values are MTProto classes; the enum name is stable.
        name = getattr(entity_type, "name", None)
        kind = (
            name.lower()
            if isinstance(name, str)
            else str(getattr(entity_type, "value", entity_type))
        )
        typed_entities.append((kind, entity))
    if settings.link_filter and (
        _LINK.search(text) or any(kind in {"url", "text_link"} for kind, _ in typed_entities)
    ):
        return "link"
    if settings.mention_limit:
        hidden = [entity for kind, entity in typed_entities if kind == "text_mention"]
        spans = [
            (entity.offset, entity.offset + entity.length)
            for entity in hidden
            if isinstance(getattr(entity, "offset", None), int)
            and isinstance(getattr(entity, "length", None), int)
        ]
        mentions = len(hidden)
        for match in _MENTION.finditer(text):
            # Telegram offsets are UTF-16 code units. Avoid counting a visible
            # @name twice when it is also a text_mention entity.
            start = len(text[: match.start()].encode("utf-16-le")) // 2
            end = len(text[: match.end()].encode("utf-16-le")) // 2
            if not any(start < right and end > left for left, right in spans):
                mentions += 1
        if mentions > settings.mention_limit:
            return "mentions"
    normalized = normalize_name(text)
    if any(keyword in normalized for keyword in settings.blocked_keywords):
        return "keyword"
    return None


class SpamGuard:
    def __init__(self, database: Any) -> None:
        self.db = database

    async def _increment(self, key: str, now: datetime, ttl: int) -> int:
        row = await self.db.rate_limits.find_one_and_update(
            {"_id": key},
            {"$inc": {"count": 1}, "$setOnInsert": {"expires_at": now + timedelta(seconds=ttl)}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(row["count"])

    async def inspect(
        self,
        group_id: int,
        user_id: int,
        text: str,
        settings: GroupSettings,
        now: datetime,
        *,
        entities: Iterable[Any] = (),
        count_activity: bool = True,
    ) -> str | None:
        reason = static_spam_reason(text[:4096], settings, entities=entities)
        if reason or not count_activity:
            return reason
        if settings.flood_enabled:
            window = settings.flood_window_seconds
            bucket = int(now.timestamp()) // window
            count = await self._increment(f"flood:{group_id}:{user_id}:{bucket}", now, window * 2)
            if count > settings.flood_limit:
                return "flood"
        if settings.duplicate_enabled and text.strip():
            digest = hashlib.sha256(normalize_name(text[:4096]).encode()).hexdigest()[:24]
            bucket = int(now.timestamp()) // 60
            count = await self._increment(
                f"duplicate:{group_id}:{user_id}:{bucket}:{digest}", now, 120
            )
            if count > settings.duplicate_limit:
                return "duplicate"
        return None
