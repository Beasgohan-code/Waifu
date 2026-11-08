"""Private, operator-authorized catalog creation and metadata edits.

All mutations use the catalog repository's audited transactions. No command
changes user ownership, deletes records, or fetches an operator-supplied URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from pydantic import ValidationError
from pymongo.errors import PyMongoError
from wzgram import Client, filters
from wzgram.enums import ChatType
from wzgram.errors import RPCError
from wzgram.types import CallbackQuery, Message

from summon.auth import TelegramUser
from summon.bot.keyboards import operator_panel_keyboard, upload_provider_keyboard
from summon.bot.presentation import Card, Emoji, callback_text, send_media_card, send_photo_card
from summon.bot.telegram_helpers import _user, callback_data, command_input, telegram_user
from summon.bot.upload_media import (
    Provider,
    menu_token,
    parse_upload,
    read_motion,
    reply_media,
    valid_token,
    valid_upload_age,
)
from summon.config import Settings
from summon.models import CharacterBasics, CharacterChanges, CharacterInput
from summon.rarities import MAX_SPAWN_WEIGHT, RARITY_WEIGHTS, Rarity, parse_rarity, rarity_label
from summon.repositories.catalog import CatalogConflict, CatalogNotFound
from summon.repositories.character_numbers import CharacterNumberingUnavailable
from summon.repositories.rarity_weights import InvalidSpawnWeights, SpawnWeightConflict
from summon.services.artwork import (
    MAX_ARTWORK_BYTES,
    MAX_ARTWORK_DIMENSION,
    MAX_ARTWORK_PIXELS,
    Artwork,
    InvalidArtwork,
    normalize_photo,
    validate_external_url,
)
from summon.services.catbox import upload_catbox
from summon.services.imgbb import ImageHostError, upload_imgbb

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


@dataclass(frozen=True, slots=True)
class PreparedArtwork:
    url: str
    stored_photo: Artwork | None = None
    channel_photo: Artwork | None = None
    external_hosted: bool = False
    telegram_file_id: str | None = None


class ImageHostAlreadyAttempted(Exception):
    """Do not replay a command whose external upload may already exist."""


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
    host_slots = asyncio.Semaphore(2)
    secret = settings.session_secret.get_secret_value()
    menu_key = (
        hashlib.sha256(("summon-upload-v1:" + secret).encode()).digest()
        if secret
        else secrets.token_bytes(32)
    )

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

    @app.on_message(filters.incoming & filters.command(["owner", "panel"]))
    async def operator_panel(_client: Client, message: Message) -> None:
        if not await authorize(message):
            return
        configured = bool(settings.webapp_url)
        await respond(
            message,
            Card(
                "Operator panel",
                (
                    "Open the Mini App and select Catalogue admin. Telegram sign-in and "
                    "server-side operator checks are required."
                    if configured
                    else "Set WEBAPP_URL to open the Mini App, or use the private catalog commands."
                ),
                emoji=Emoji.ADMIN_SHIELD,
            ),
            markup=(
                operator_panel_keyboard(settings.webapp_url, premium=settings.premium_emoji_enabled)
                if configured
                else None
            ),
        )

    @app.on_message(filters.incoming & filters.command(["sudolist"]))
    async def operator_list(_client: Client, message: Message) -> None:
        if not await authorize(message):
            return
        operators = settings.operator_ids
        await respond(
            message,
            Card(
                "Configured operators",
                "Read-only server configuration; permissions cannot be changed in chat. "
                f"Showing {min(len(operators), 20)} of {len(operators)}.",
                emoji=Emoji.ADMIN_SHIELD,
                fields=tuple(
                    (f"Operator {i}", str(uid)) for i, uid in enumerate(operators[:20], 1)
                ),
            ),
        )

    @app.on_message(filters.incoming & filters.command(["chance"]))
    async def change_spawn_weight(_client: Client, message: Message) -> None:
        user = await authorize(message)
        if user is None:
            return
        parts = (command_input(message).args or "").split()
        rarity = parse_rarity(parts[0]) if len(parts) == 2 else None
        if (
            rarity is None
            or rarity not in RARITY_WEIGHTS
            or len(parts) != 2
            or re.fullmatch(r"[0-9]{1,7}", parts[1]) is None
            or int(parts[1]) > MAX_SPAWN_WEIGHT
        ):
            await respond(
                message,
                Card(
                    "Set a summon weight",
                    "Use /chance rarity-number weight, for example /chance 14 10. "
                    f"Weights range from 0 to {MAX_SPAWN_WEIGHT}; 0 disables a bucket. "
                    "Legacy Epic shares Special Edition's weight.",
                    emoji=Emoji.RARITY_STAR,
                ),
            )
            return
        identity = getattr(app, "me", None)
        if not identity or not isinstance(identity.id, int) or identity.id <= 0:
            await respond(
                message,
                Card("Catalog unavailable", "Bot identity is not ready.", emoji=Emoji.WARNING),
            )
            return
        weight = int(parts[1])
        event_id = _event_id(identity.id, message.chat.id, message.id)
        try:
            old, new = await store.catalog.set_spawn_weight(
                rarity, weight, actor_id=user.id, event_id=event_id
            )
        except SpawnWeightConflict:
            await respond(
                message,
                Card(
                    "Weight update not applied",
                    "The command was already processed or conflicted. Check /chancelist.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        except InvalidSpawnWeights:
            await respond(
                message,
                Card(
                    "Weight configuration invalid",
                    "An operator must inspect the saved data.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        except (PyMongoError, OSError, TimeoutError) as exc:
            logger.warning("Spawn-weight change unconfirmed (%s)", type(exc).__name__)
            await respond(
                message,
                Card(
                    "Result unconfirmed",
                    "Check /chancelist before trying again.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        await respond(
            message,
            Card(
                "Summon weight saved",
                "Weights apply to enabled group spawns. In-flight selections may use prior values; "
                "empty buckets are skipped. This does not change ownership.",
                emoji=Emoji.DONE,
                fields=(("Rarity", rarity_label(rarity)), ("Weight", f"{old} → {new}")),
            ),
        )

    async def artwork_for(message: Message, url: str | None, event_id: str) -> PreparedArtwork:
        photo_message = _photo_message(message)
        if bool(photo_message) == bool(url):
            raise InvalidArtwork("Choose either one Telegram photo or one HTTPS URL")
        if not photo_message:
            return PreparedArtwork(validate_external_url(url or ""))
        if settings.artwork_backend != "imgbb":
            raise InvalidArtwork("Use /upload to choose a public media host")
        file_id = getattr(photo_message.photo, "file_id", None)
        if (
            not isinstance(file_id, str)
            or not 1 <= len(file_id) <= 2048
            or not file_id.isascii()
            or not file_id.isprintable()
        ):
            raise InvalidArtwork("Telegram photo ID is unavailable")
        artwork = await _read_photo(app, photo_message, photo_slots)
        # An external provider has no transactional or idempotency API. A durable,
        # majority-acknowledged claim prevents replay for seven days; the catalog
        # audit also blocks successfully completed commands beyond that window.
        if await store.catalog.catalog_event_exists(event_id):
            raise ImageHostAlreadyAttempted
        if not await store.limiter.claim_once(f"imgbb:{event_id}", seconds=7 * 86400):
            raise ImageHostAlreadyAttempted
        try:
            async with asyncio.timeout(40):
                async with host_slots:
                    hosted_url = await upload_imgbb(artwork, settings.imgbb_key)
        except TimeoutError:
            raise ImageHostError("Image host result is unconfirmed") from None
        return PreparedArtwork(
            hosted_url, channel_photo=artwork, external_hosted=True, telegram_file_id=file_id
        )

    async def archive_photo(artwork: Artwork | None, character_id: str) -> bool:
        if settings.db_channel is None or artwork is None:
            return True
        try:
            async with asyncio.timeout(30):
                channel = await app.get_chat(settings.db_channel)
                if channel.type != ChatType.CHANNEL:
                    logger.warning("Configured artwork archive is not a Telegram channel")
                    return False
                photo = BytesIO(artwork.content)
                photo.name = "summon-artwork.jpg"
                sent = await send_photo_card(
                    app,
                    settings.db_channel,
                    photo,
                    Card(
                        "Catalog artwork archived",
                        "A public catalog photo was saved by an operator.",
                        emoji=Emoji.OPEN_COLLECTION,
                        fields=(("ID", character_id),),
                    ),
                    premium=settings.premium_emoji_enabled,
                )
                return sent is not None
        except (RPCError, OSError, TimeoutError) as exc:
            # A timeout may have posted the copy. Never retry an ambiguous send.
            logger.warning("Catalog archive delivery unconfirmed (%s)", type(exc).__name__)
            return False

    async def upload_source(message: Message) -> Message:
        if message.reply_to_message is not None:
            return message.reply_to_message
        if message.reply_to_message_id is not None:
            source = await app.get_messages(
                message.chat.id, message_ids=message.reply_to_message_id, replies=0
            )
            if isinstance(source, Message) and not source.empty:
                return source
        # Keep supporting an ordinary captioned photo from the earlier flow.
        if message.photo or message.video or message.animation:
            return message
        raise InvalidArtwork("Reply to a photo, video or GIF")

    async def start_media_upload(
        message: Message, user: TelegramUser, bot_id: int, event_id: str
    ) -> None:
        try:
            parsed = parse_upload(command_input(message).args or "", _generated_id("", event_id))
            source = await upload_source(message)
            media = reply_media(source)
            if not valid_upload_age(message):
                raise InvalidArtwork("Upload menu expired")
            if settings.db_channel is None:
                await respond(
                    message,
                    Card(
                        "Archive channel needed",
                        "Set DB_CHANNEL to a private channel where the bot can post, "
                        "then try again.",
                        emoji=Emoji.WARNING,
                    ),
                )
                return
            providers: list[tuple[str, Provider]] = [("Catbox", "catbox")]
            if media.kind == "photo" and settings.imgbb_key.get_secret_value().strip():
                providers.append(("ImgBB", "imgbb"))
            choices = [
                (
                    label,
                    f"catalog-upload:{message.id}:{'c' if provider == 'catbox' else 'i'}:"
                    f"{menu_token(menu_key, bot_id, user.id, message, media, provider)}",
                )
                for label, provider in providers
            ]
        except (ValueError, ValidationError, RPCError, OSError, TimeoutError):
            await respond(
                message,
                Card(
                    "Check the character details",
                    'Reply to a photo, video or GIF with /upload "Character name" '
                    '"Anime name" 1, or use /upload Character | Anime | 1. '
                    "Rarity numbers are 1–18; video and GIF use AMV Edition.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        rarity = "amv-edition" if media.kind != "photo" else parsed.rarity
        await respond(
            message,
            Card(
                "Choose a media host",
                "The selected file becomes public. Catbox accepts photos and moving media; "
                "ImgBB is available for photos when its key is configured. "
                "Videos and GIFs may retain embedded metadata. "
                "The channel copy and provider upload cannot be undone automatically.",
                emoji=Emoji.OPEN_COLLECTION,
                fields=(
                    ("Character", parsed.name),
                    ("Series", parsed.category),
                    ("Rarity", rarity_label(rarity)),
                ),
            ),
            markup=upload_provider_keyboard(choices, premium=settings.premium_emoji_enabled),
        )

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
        if command.command == "upload" and (
            message.reply_to_message_id is not None
            or message.reply_to_message is not None
            or message.photo is not None
            or message.video is not None
            or message.animation is not None
        ):
            await start_media_upload(message, user, identity.id, event_id)
            return
        prepared: PreparedArtwork | None = None
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
            await store.catalog.prepare_numbering()
            if (
                settings.artwork_backend == "imgbb"
                and _photo_message(message) is not None
                and not parsed.url
                and await store.catalog.character_exists(basics.id)
            ):
                raise CatalogConflict
            prepared = await artwork_for(message, parsed.url, event_id)
            data = CharacterInput(
                **basics.model_dump(),
                image_url=prepared.url,
                telegram_file_id=prepared.telegram_file_id,
            )
            await store.catalog.create_character(
                data, actor_id=user.id, event_id=event_id, artwork=prepared.stored_photo
            )
        except ImageHostAlreadyAttempted:
            await respond(
                message,
                Card(
                    "Upload already attempted",
                    "Check the catalog and ImgBB before sending a new command.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        except ImageHostError:
            await respond(
                message,
                Card(
                    "Image host unconfirmed",
                    "The catalog was not changed. A public ImgBB upload may exist; "
                    "check ImgBB before trying again.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        except (ValueError, ValidationError):
            await respond(
                message,
                Card(
                    "Check the character details",
                    'Reply to a photo, video or GIF with /upload "Character name" '
                    '"Anime name" 1, or use /upload Character | Anime | 1. '
                    "To add an HTTPS URL directly, use /addchar name | anime | rarity | URL. "
                    "Photos are limited to 3 MiB; videos and GIFs to 16 MiB.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        except CharacterNumberingUnavailable:
            await respond(
                message,
                Card(
                    "Numbering needs review",
                    "An operator must review the existing catalog; no ownership was changed.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        except CatalogConflict:
            await respond(
                message,
                Card(
                    "Already exists",
                    "This ID exists or the command was already processed."
                    + (
                        " An ImgBB image may already exist; check it before retrying."
                        if prepared and prepared.external_hosted
                        else ""
                    ),
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
                    "Check the catalog before sending another command."
                    + (
                        " The ImgBB image may also exist."
                        if prepared and prepared.external_hosted
                        else ""
                    ),
                    emoji=Emoji.WARNING,
                ),
            )
            return
        if prepared is None:
            raise RuntimeError("Catalog artwork was not prepared")
        archived = await archive_photo(prepared.channel_photo, data.id)
        await respond(
            message,
            Card(
                "Character added",
                "Artwork is public. Ownership was not changed."
                + (
                    " Channel copy unconfirmed; check the channel before reposting."
                    if not archived
                    else ""
                ),
                emoji=Emoji.DONE,
                fields=(("ID", data.id), ("Rarity", rarity_label(data.rarity))),
            ),
        )

    async def archive_media(
        payload: bytes, extension: str, media_type: str, original_file_id: str, character_id: str
    ) -> tuple[str, bool]:
        if settings.db_channel is None:
            return original_file_id, False
        try:
            async with asyncio.timeout(120):
                channel = await app.get_chat(settings.db_channel)
                if channel.type != ChatType.CHANNEL:
                    return original_file_id, False
                file = BytesIO(payload)
                file.name = f"summon.{extension}"
                sent = await send_media_card(
                    app,
                    settings.db_channel,
                    file,
                    Card(
                        "Catalog media archived",
                        "A character's public media was added by an operator.",
                        emoji=Emoji.OPEN_COLLECTION,
                        fields=(("ID", character_id),),
                    ),
                    media_type=media_type,
                    premium=settings.premium_emoji_enabled,
                )
                if sent is None:
                    return original_file_id, False
                item = getattr(sent, media_type, None)
                channel_file_id = getattr(item, "file_id", None)
                if (
                    isinstance(channel_file_id, str)
                    and 1 <= len(channel_file_id) <= 2048
                    and channel_file_id.isascii()
                    and channel_file_id.isprintable()
                ):
                    return channel_file_id, True
                return original_file_id, True
        except (RPCError, OSError, TimeoutError) as exc:
            # A timeout may have posted the copy. Never repeat an uncertain send.
            logger.warning("Catalog archive delivery unconfirmed (%s)", type(exc).__name__)
            return original_file_id, False

    @app.on_callback_query(filters.regex("^catalog-upload:") | filters.regex(b"^catalog-upload:"))
    async def upload_choice(_client: Client, callback: CallbackQuery) -> None:
        actor = telegram_user(callback.from_user)
        parts = callback_data(callback).split(":")
        identity = getattr(app, "me", None)
        chat = getattr(callback.message, "chat", None)
        kind: Any = getattr(chat, "type", None)
        if (
            actor is None
            or actor.id not in settings.operator_ids
            or not identity
            or not isinstance(identity.id, int)
            or chat is None
            or chat.id != actor.id
            or getattr(kind, "value", kind) != "private"
            or len(parts) != 4
            or not re.fullmatch(r"[1-9][0-9]{0,9}", parts[1])
            or parts[2] not in {"c", "i"}
            or not re.fullmatch(r"[a-zA-Z0-9_-]{16}", parts[3])
        ):
            await callback.answer(
                callback_text("This menu is unavailable.", emoji=Emoji.WARNING), show_alert=True
            )
            return
        provider: Provider = "catbox" if parts[2] == "c" else "imgbb"
        try:
            command_message = await app.get_messages(actor.id, message_ids=int(parts[1]), replies=1)
            if not isinstance(command_message, Message) or command_message.empty:
                raise ValueError("Original command is unavailable")
            if (
                not _valid_private_message(command_message, actor)
                or not command_message.from_user
                or command_message.from_user.id != actor.id
                or command_message.forward_origin is not None
                or command_message.via_bot is not None
                or command_message.business_connection_id is not None
                or command_message.sender_business_bot is not None
                or not valid_upload_age(command_message)
            ):
                raise ValueError("Original command is unavailable")
            command_text = str(command_message.text or command_message.caption or "").split(
                maxsplit=1
            )
            command_name, _, mention = (command_text[0] if command_text else "").partition("@")
            if command_name.casefold() != "/upload" or (
                mention and mention.casefold() != (identity.username or "").casefold()
            ):
                raise ValueError("Original command changed")
            event_id = _event_id(identity.id, actor.id, command_message.id)
            parsed = parse_upload(
                command_input(command_message).args or "", _generated_id("", event_id)
            )
            source = await upload_source(command_message)
            media = reply_media(source)
            expected = menu_token(menu_key, identity.id, actor.id, command_message, media, provider)
            if not valid_token(expected, parts[3]):
                raise ValueError("Original command or media changed")
            if provider == "imgbb" and (
                media.kind != "photo" or not settings.imgbb_key.get_secret_value().strip()
            ):
                raise ValueError("Provider is unavailable")
            if settings.db_channel is None:
                raise ValueError("Archive channel is unavailable")
            if not await store.limiter.allow(
                f"catalog:{actor.id}:upload-choice", limit=10, seconds=60
            ):
                await callback.answer(
                    callback_text("Try again shortly.", emoji=Emoji.HOURGLASS), show_alert=True
                )
                return
            await store.catalog.prepare_numbering()
            if await store.catalog.character_exists(parsed.character_id):
                raise CatalogConflict
            if await store.catalog.catalog_event_exists(event_id):
                raise ImageHostAlreadyAttempted
            if not await store.limiter.claim_once(f"catalog-media:{event_id}", seconds=7 * 86400):
                raise ImageHostAlreadyAttempted
        except (ValueError, ValidationError, RPCError, OSError, TimeoutError):
            await callback.answer(
                callback_text(
                    "This menu expired or its original message changed. Send /upload again.",
                    emoji=Emoji.WARNING,
                ),
                show_alert=True,
            )
            return
        except (CharacterNumberingUnavailable, CatalogConflict, ImageHostAlreadyAttempted):
            await callback.answer(
                callback_text(
                    "Already attempted or catalog numbering needs review. Check the catalog.",
                    emoji=Emoji.WARNING,
                ),
                show_alert=True,
            )
            return
        except PyMongoError as exc:
            logger.warning("Catalog upload preflight unavailable (%s)", type(exc).__name__)
            await callback.answer(
                callback_text("Catalog unavailable; try later.", emoji=Emoji.WARNING),
                show_alert=True,
            )
            return

        await callback.answer(
            callback_text("Uploading publicly; please wait.", emoji=Emoji.HOURGLASS)
        )
        try:
            artwork = None
            if media.kind == "photo":
                artwork = await _read_photo(app, source, photo_slots)
                payload, extension = artwork.content, "jpg"
            else:
                payload, extension = await read_motion(app, source, media, photo_slots)
            async with host_slots:
                if provider == "imgbb":
                    if artwork is None:
                        raise ImageHostError("Photo required")
                    url = await upload_imgbb(artwork, settings.imgbb_key)
                else:
                    url = await upload_catbox(payload, extension, settings.catbox_userhash)
            backup_id, archived = await archive_media(
                payload, extension, media.kind, media.file_id, parsed.character_id
            )
            rarity = "amv-edition" if media.kind != "photo" else parsed.rarity
            data = CharacterInput(
                id=parsed.character_id,
                name=parsed.name,
                category=parsed.category,
                rarity=rarity,
                image_url=url,
                media_type=media.kind,
                telegram_file_id=backup_id,
            )
            saved = await store.catalog.create_character(data, actor_id=actor.id, event_id=event_id)
        except (
            ImageHostError,
            ValueError,
            ValidationError,
            RPCError,
            OSError,
            TimeoutError,
            PyMongoError,
            CatalogConflict,
            CharacterNumberingUnavailable,
        ) as exc:
            logger.warning("Public catalog upload unconfirmed (%s)", type(exc).__name__)
            await respond(
                callback.message,
                Card(
                    "Upload result unconfirmed",
                    "Check the catalog, selected host and DB_CHANNEL before trying again. "
                    "A public upload or channel copy may already exist; "
                    "this button will not retry it.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        number = saved.get("number") if isinstance(saved, dict) else None
        await respond(
            callback.message,
            Card(
                "Character added",
                "The hosted media URL and Telegram file ID were saved; ownership was not changed."
                + (
                    " Channel copy unconfirmed; check DB_CHANNEL before reposting."
                    if not archived
                    else ""
                ),
                emoji=Emoji.DONE,
                fields=(("ID", str(number or data.id)), ("Rarity", rarity_label(data.rarity))),
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
        event_id = _event_id(identity.id, message.chat.id, message.id)
        prepared: PreparedArtwork | None = None
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
            if field == "image_url":
                if (
                    settings.artwork_backend == "imgbb"
                    and _photo_message(message) is not None
                    and not value
                    and not await store.catalog.character_exists(character_id)
                ):
                    raise CatalogNotFound
                prepared = await artwork_for(message, value or None, event_id)
                value = prepared.url
            elif not value or _photo_message(message) is not None:
                raise ValueError("Metadata value required")
            elif field == "rarity":
                parsed = parse_rarity(value)
                if parsed is None:
                    raise ValueError("Invalid rarity")
                value = parsed
            changes = CharacterChanges.model_validate({field: value})
            media_id = prepared.telegram_file_id if prepared else None
            await store.catalog.update_character(
                character_id,
                changes,
                actor_id=user.id,
                event_id=event_id,
                artwork=prepared.stored_photo if prepared else None,
                **({"telegram_file_id": media_id} if media_id else {}),
            )
        except ImageHostAlreadyAttempted:
            await respond(
                message,
                Card(
                    "Upload already attempted",
                    "Check the catalog and ImgBB before sending a new command.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        except ImageHostError:
            await respond(
                message,
                Card(
                    "Image host unconfirmed",
                    "The catalog was not changed. A public ImgBB upload may exist; "
                    "check ImgBB before trying again.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        except (ValueError, ValidationError):
            await respond(
                message,
                Card(
                    "Check the update",
                    "Use /updatechar id name|series|rarity|image|description value. "
                    "Images accept a public HTTPS URL; replied photos require an "
                    "operator-configured ImgBB backend and key.",
                    emoji=Emoji.WARNING,
                ),
            )
            return
        except CatalogNotFound:
            await respond(
                message,
                Card(
                    "Not found",
                    "That character ID does not exist."
                    + (
                        " An ImgBB image may already exist; check it before retrying."
                        if prepared and prepared.external_hosted
                        else ""
                    ),
                    emoji=Emoji.SEARCH,
                ),
            )
            return
        except CatalogConflict:
            await respond(
                message,
                Card(
                    "Already processed",
                    "This command was already applied."
                    + (
                        " An ImgBB image may already exist; check it before retrying."
                        if prepared and prepared.external_hosted
                        else ""
                    ),
                    emoji=Emoji.WARNING,
                ),
            )
            return
        except (PyMongoError, RPCError, ConnectionError, OSError, TimeoutError) as exc:
            logger.warning("Catalog update could not be confirmed (%s)", type(exc).__name__)
            await respond(
                message,
                Card(
                    "Result unconfirmed",
                    "Check the catalog before sending another command."
                    + (
                        " The ImgBB image may also exist."
                        if prepared and prepared.external_hosted
                        else ""
                    ),
                    emoji=Emoji.WARNING,
                ),
            )
            return
        archived = await archive_photo(prepared.channel_photo if prepared else None, character_id)
        await respond(
            message,
            Card(
                "Character updated",
                "Ownership and character ID were not changed."
                + (
                    " Channel copy unconfirmed; check the channel before reposting."
                    if not archived
                    else ""
                ),
                emoji=Emoji.DONE,
                fields=(("ID", character_id), ("Updated", field)),
            ),
        )
