"""Private, operator-authorized catalog creation and metadata edits.

All mutations use the catalog repository's audited transactions. No command
changes user ownership, deletes records, or fetches an operator-supplied URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from pymongo.errors import PyMongoError
from wzgram import Client, filters
from wzgram.errors import RPCError
from wzgram.types import Message

from summon.auth import TelegramUser
from summon.bot.presentation import Card, Emoji
from summon.bot.telegram_helpers import _user, command_input
from summon.config import Settings
from summon.models import CharacterBasics, CharacterChanges, CharacterInput
from summon.rarities import Rarity, parse_rarity, rarity_label
from summon.repositories.catalog import CatalogConflict, CatalogNotFound
from summon.services.artwork import (
    MAX_ARTWORK_BYTES,
    MAX_ARTWORK_DIMENSION,
    MAX_ARTWORK_PIXELS,
    Artwork,
    InvalidArtwork,
    normalize_photo,
    validate_external_url,
)

logger = logging.getLogger(__name__)
_CHARACTER_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_MAX_ARGUMENTS = 4096


@dataclass(frozen=True, slots=True)
class NewCharacter:
    character_id: str
    name: str
    category: str
    rarity: Rarity
    url: str | None


def _event_id(bot_id: int, chat_id: int, message_id: int) -> str:
    material = f"{bot_id}:{chat_id}:{message_id}".encode("ascii")
    return "telegram-catalog:" + hashlib.sha256(material).hexdigest()


def _generated_id(name: str, event_id: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    prefix = re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-")[:48].rstrip("-")
    return f"{prefix or 'character'}-{event_id[-12:]}"


def _new_character(command: str, args: str, event_id: str) -> NewCharacter:
    if len(args) > _MAX_ARGUMENTS:
        raise ValueError("Too many characters")
    if command == "upload":
        parts = [part.strip() for part in args.split("|")]
        if len(parts) not in (4, 5):
            raise ValueError("Invalid upload format")
        character_id, name, category, rarity, *url = parts
    elif "|" in args:
        parts = [part.strip() for part in args.split("|")]
        if len(parts) not in (3, 4):
            raise ValueError("Invalid addchar format")
        name, category, rarity, *url = parts
        character_id = _generated_id(name, event_id)
    else:
        parts = args.split(maxsplit=3)
        if len(parts) not in (3, 4):
            raise ValueError("Invalid addchar format")
        name, category, rarity, *url = parts
        name, category = name.replace("_", " "), category.replace("_", " ")
        character_id = _generated_id(name, event_id)
    parsed = parse_rarity(rarity)
    if not parsed:
        raise ValueError("Invalid rarity")
    return NewCharacter(character_id, name, category, parsed, url[0] if url else None)


def _photo_message(message: Message) -> Message | None:
    if message.photo is not None:
        return message
    reply = message.reply_to_message
    return reply if reply is not None and reply.photo is not None else None


async def _read_photo(client: Client, message: Message, slots: asyncio.Semaphore) -> Artwork:
    photo = message.photo
    if photo is None:
        raise InvalidArtwork("Send an ordinary Telegram photo")
    size, width, height = photo.file_size, photo.width, photo.height
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 < size <= MAX_ARTWORK_BYTES
        or not isinstance(width, int)
        or not isinstance(height, int)
        or not 0 < width <= MAX_ARTWORK_DIMENSION
        or not 0 < height <= MAX_ARTWORK_DIMENSION
        or width * height > MAX_ARTWORK_PIXELS
    ):
        raise InvalidArtwork("Photo must be at most 3 MiB and within the dimension limits")
    if (
        message.ephemeral_message_id is not None
        or message.has_protected_content
        or message.has_media_spoiler
        or photo.ttl_seconds is not None
    ):
        raise InvalidArtwork("Protected, spoiler and self-destructing photos cannot be published")
    # Telegram chunks are at most 1 MiB; one extra chunk detects incorrect size
    # metadata without allowing the bot to buffer an unbounded download.
    async with slots:
        payload = bytearray()
        async with asyncio.timeout(30):
            async for chunk in client.stream_media(message, limit=4):
                if not isinstance(chunk, bytes) or len(chunk) > MAX_ARTWORK_BYTES - len(payload):
                    raise InvalidArtwork("Photo is too large")
                payload.extend(chunk)
        if len(payload) != size:
            raise InvalidArtwork("Telegram did not provide the complete photo")
        return await normalize_photo(bytes(payload))


def _valid_private_message(message: Message, user: TelegramUser) -> bool:
    kind: Any = getattr(message.chat, "type", None)
    return bool(
        message.chat
        and getattr(kind, "value", kind) == "private"
        and message.chat.id == user.id
        and isinstance(message.id, int)
        and message.id > 0
        and message.ephemeral_message_id is None
    )


def register_catalog_commands(app: Client, store: Any, settings: Settings, respond: Any) -> None:
    photo_slots = asyncio.Semaphore(2)

    async def authorize(message: Message) -> TelegramUser | None:
        user = _user(message)
        if not user:
            return None
        if user.id not in settings.operator_ids:
            await respond(
                message,
                Card(
                    "Operator access required",
                    "Only configured catalog operators can do that.",
                    emoji=Emoji.PRIVATE,
                ),
                private=True,
            )
            return None
        if not _valid_private_message(message, user):
            await respond(
                message,
                Card(
                    "Use a private chat",
                    "Catalog changes are accepted only in your private chat.",
                    emoji=Emoji.PRIVATE,
                ),
                private=True,
            )
            return None
        try:
            allowed = await store.limiter.allow(f"catalog:{user.id}:command", limit=10, seconds=60)
        except (PyMongoError, ConnectionError, OSError, TimeoutError) as exc:
            logger.warning("Catalog rate check unavailable (%s)", type(exc).__name__)
            await respond(
                message, Card("Catalog unavailable", "Please try again later.", emoji=Emoji.WARNING)
            )
            return None
        if not allowed:
            await respond(
                message, Card("Take a breath", "Try again in a minute.", emoji=Emoji.HOURGLASS)
            )
            return None
        return user

    async def artwork_for(message: Message, url: str | None) -> tuple[str, Artwork | None]:
        photo_message = _photo_message(message)
        if bool(photo_message) == bool(url):
            raise InvalidArtwork("Choose either one Telegram photo or one HTTPS URL")
        if photo_message:
            artwork = await _read_photo(app, photo_message, photo_slots)
            return artwork.path, artwork
        return validate_external_url(url or ""), None

    @app.on_message(filters.incoming & filters.command(["upload", "addchar"]))
    async def create_character(_client: Client, message: Message) -> None:
        user = await authorize(message)
        if user is None:
            return
        identity = getattr(app, "me", None)
        if not identity or not isinstance(identity.id, int) or identity.id <= 0:
            await respond(
                message,
                Card("Catalog unavailable", "Bot identity is not ready.", emoji=Emoji.WARNING),
            )
            return
        command = command_input(message)
        event_id = _event_id(identity.id, message.chat.id, message.id)
        try:
            parsed = _new_character(command.command, command.args or "", event_id)
            # Reject malformed metadata before consuming Telegram bandwidth or a
            # bounded image-processing slot. Reuse the same validators as the API.
            basics = CharacterBasics(
                id=parsed.character_id,
                name=parsed.name,
                category=parsed.category,
                rarity=parsed.rarity,
            )
            image_url, image = await artwork_for(message, parsed.url)
            data = CharacterInput(**basics.model_dump(), image_url=image_url)
            await store.catalog.create_character(
                data, actor_id=user.id, event_id=event_id, artwork=image
            )
        except (ValueError, ValidationError):
            await respond(
                message,
                Card(
                    "Check the character details",
                    "Use /upload id | name | series | rarity | HTTPS-URL, or reply to a photo "
                    "with /upload id | name | series | rarity. "
                    "For /addchar, omit the ID. Use a rarity ID from 1 to 18 or a rarity name. "
                    "Photos must be ordinary JPEG photos up to 3 MiB.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        except CatalogConflict:
            await respond(
                message,
                Card(
                    "Already exists",
                    "This ID exists or the command was already processed.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        except (PyMongoError, RPCError, ConnectionError, OSError, TimeoutError) as exc:
            logger.warning("Catalog creation could not be confirmed (%s)", type(exc).__name__)
            await respond(
                message,
                Card(
                    "Result unconfirmed",
                    "Check the catalog before sending another command.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        await respond(
            message,
            Card(
                "Character added",
                "Artwork is public. Ownership was not changed.",
                emoji=Emoji.DONE,
                fields=(("ID", data.id), ("Rarity", rarity_label(data.rarity))),
            ),
        )

    @app.on_message(filters.incoming & filters.command(["updatechar", "update"]))
    async def edit_character(_client: Client, message: Message) -> None:
        user = await authorize(message)
        if user is None:
            return
        identity = getattr(app, "me", None)
        if not identity or not isinstance(identity.id, int) or identity.id <= 0:
            await respond(
                message,
                Card("Catalog unavailable", "Bot identity is not ready.", emoji=Emoji.WARNING),
            )
            return
        args = command_input(message).args or ""
        parts = args.split(maxsplit=2)
        try:
            if (
                len(args) > _MAX_ARGUMENTS
                or len(parts) < 2
                or not _CHARACTER_ID.fullmatch(parts[0])
            ):
                raise ValueError("Invalid character ID")
            character_id, field = parts[:2]
            value = parts[2].strip() if len(parts) == 3 else ""
            field = {"series": "category", "anime": "category", "image": "image_url"}.get(
                field, field
            )
            if field not in {"name", "category", "rarity", "image_url", "description"}:
                raise ValueError("Invalid metadata field")
            image = None
            if field == "image_url":
                value, image = await artwork_for(message, value or None)
            elif not value or _photo_message(message) is not None:
                raise ValueError("Metadata value required")
            elif field == "rarity":
                parsed = parse_rarity(value)
                if parsed is None:
                    raise ValueError("Invalid rarity")
                value = parsed
            changes = CharacterChanges.model_validate({field: value})
            event_id = _event_id(identity.id, message.chat.id, message.id)
            await store.catalog.update_character(
                character_id, changes, actor_id=user.id, event_id=event_id, artwork=image
            )
        except (ValueError, ValidationError):
            await respond(
                message,
                Card(
                    "Check the update",
                    "Use /updatechar id name|series|rarity|image|description value. "
                    "For an image, provide a public HTTPS URL or reply to a photo without a URL.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        except CatalogNotFound:
            await respond(
                message, Card("Not found", "That character ID does not exist.", emoji=Emoji.SEARCH)
            )
            return
        except CatalogConflict:
            await respond(
                message,
                Card("Already processed", "This command was already applied.", emoji=Emoji.WARNING),
            )
            return
        except (PyMongoError, RPCError, ConnectionError, OSError, TimeoutError) as exc:
            logger.warning("Catalog update could not be confirmed (%s)", type(exc).__name__)
            await respond(
                message,
                Card(
                    "Result unconfirmed",
                    "Check the catalog before sending another command.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        await respond(
            message,
            Card(
                "Character updated",
                "Ownership and character ID were not changed.",
                emoji=Emoji.DONE,
                fields=(("ID", character_id), ("Updated", field)),
            ),
        )
