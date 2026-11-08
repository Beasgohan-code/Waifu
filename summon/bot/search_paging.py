"""Actor-bound, short-lived Telegram catalog search navigation.

The callback never contains the search text. It authenticates the target page
against the original private command, which is re-fetched before every query.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta
from typing import Any

from wzgram.types import Message

from summon.bot.presentation import Card, Emoji
from summon.bot.telegram_helpers import recent_message
from summon.rarities import rarity_label

SEARCH_PAGE_SIZE = 10
SEARCH_MAX_PAGES = 25
SEARCH_LIFETIME = timedelta(minutes=15)


def search_key(secret: str) -> bytes:
    return (
        hashlib.sha256(b"summon-catalog-search-v1\0" + secret.encode()).digest()
        if secret
        else secrets.token_bytes(32)
    )


def search_term(args: str | None) -> str | None:
    term = (args or "").strip()
    if (
        not term
        or len(term) > 80
        or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in term)
    ):
        return None
    return term


def source_term(
    message: Message, actor_id: int, bot_username: str | None, *, now: datetime | None = None
) -> str | None:
    """Validate a re-fetched private command before trusting its search text."""
    chat = message.chat
    kind: Any = getattr(chat, "type", None)
    if (
        not chat
        or chat.id != actor_id
        or getattr(kind, "value", kind) != "private"
        or not message.from_user
        or message.from_user.id != actor_id
        or message.from_user.is_bot
        or message.sender_chat is not None
        or message.forward_origin is not None
        or message.via_bot is not None
        or message.business_connection_id is not None
        or message.sender_business_bot is not None
        or message.ephemeral_message_id is not None
        or not isinstance(message.id, int)
        or isinstance(message.id, bool)
        or not 0 < message.id < 10**10
        or not recent_message(message, lifetime=SEARCH_LIFETIME, now=now)
        or not isinstance(message.text, str)
    ):
        return None
    pieces = message.text.split(maxsplit=1)
    if len(pieces) != 2:
        return None
    command, _, mention = pieces[0].partition("@")
    if command.casefold() not in {"/search", "/find", "/sips"} or (
        mention and mention.casefold() != (bot_username or "").casefold()
    ):
        return None
    return search_term(pieces[1])


def search_callback(key: bytes, bot_id: int, actor_id: int, message: Message, page: int) -> str:
    if (
        not 0 < bot_id < 2**53
        or not 0 < actor_id < 2**53
        or not isinstance(message.id, int)
        or isinstance(message.id, bool)
        or not 0 < message.id < 10**10
        or not isinstance(message.text, str)
        or not 1 <= page <= SEARCH_MAX_PAGES
    ):
        raise ValueError("Invalid catalog search navigation")
    material = "\0".join(
        (str(bot_id), str(actor_id), str(message.id), message.text, str(page))
    ).encode("utf-8")
    digest = hmac.new(key, b"summon-search-page-v1\0" + material, hashlib.sha256).digest()[:12]
    token = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"catalog-search:{message.id}:{page}:{token}"


def parse_search_callback(data: str) -> tuple[int, int, str] | None:
    match = re.fullmatch(
        r"catalog-search:([1-9][0-9]{0,9}):([1-9][0-9]?):([A-Za-z0-9_-]{16})", data
    )
    if not match:
        return None
    message_id, page = int(match[1]), int(match[2])
    if page > SEARCH_MAX_PAGES:
        return None
    return message_id, page, match[3]


def valid_search_callback(expected: str, actual: str) -> bool:
    return hmac.compare_digest(expected, actual)


def search_card(result: dict[str, Any], page: int, *, can_navigate: bool) -> Card:
    """Show bounded public metadata without exposing another collector's inventory."""

    def brief(value: str, length: int) -> str:
        return value if len(value) <= length else value[: length - 1] + "…"

    fields = []
    for item in result["items"][:SEARCH_PAGE_SIZE]:
        number = item.get("number")
        valid_number = (
            isinstance(number, int) and not isinstance(number, bool) and 0 < number < 2**53
        )
        key = item.get("id")
        valid_key = isinstance(key, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", key)
        if valid_number and valid_key and str(number) != key:
            shown_id = f"{number} (key: {key})"
        elif valid_number:
            shown_id = str(number)
        elif valid_key:
            shown_id = key
        else:
            shown_id = "ID needs review"
        name, category, rarity = item.get("name"), item.get("category"), item.get("rarity")
        name = brief(name, 48) if isinstance(name, str) and name else "Unnamed character"
        category = (
            brief(category, 42) if isinstance(category, str) and category else "Unknown series"
        )
        rarity = brief(rarity_label(rarity), 32) if isinstance(rarity, str) else "Uncategorized"
        fields.append((name, f"{shown_id} · {category} · {rarity}"))
    total = result["total"]
    if fields:
        body = (
            f"Page {page} · {len(fields)} shown of {total} matches. "
            "Search matches name prefixes; results can change between pages."
        )
        if total > SEARCH_PAGE_SIZE * SEARCH_MAX_PAGES:
            body += " Refine the prefix to browse beyond the first 250 matches."
        if not can_navigate and total > SEARCH_PAGE_SIZE:
            body += " Open a private chat or the Mini App for more results."
    elif page == 1:
        body = "No matching characters found. Search matches the beginning of a name."
    else:
        body = "No matches on this page; the catalog may have changed. Go back or search again."
    return Card("Character search", body, emoji=Emoji.SEARCH, fields=tuple(fields))
