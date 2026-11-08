"""Private, bounded Telegram media uploads and signed provider menus."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from wzgram import Client
from wzgram.types import Message

from summon.models import CharacterBasics
from summon.rarities import RARITIES, Rarity, parse_rarity
from summon.repositories.limits import RateLimiter
from summon.services.artwork import MAX_ARTWORK_BYTES, InvalidArtwork
from summon.services.catbox import MAX_MEDIA_BYTES, valid_media_signature

MediaKind = Literal["photo", "video", "animation"]
Provider = Literal["catbox", "imgbb"]
UploadPurpose = Literal["choose", "publish", "cancel"]
UPLOAD_LIFETIME = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class UploadDetails:
    character_id: str
    name: str
    category: str
    rarity: Rarity


@dataclass(frozen=True, slots=True)
class ReplyMedia:
    kind: MediaKind
    file_id: str
    unique_id: str
    size: int


def parse_upload(args: str, generated_id: str) -> UploadDetails:
    """Accept pipe fields or shell-style quoted multiword names, not guesses."""
    if len(args) > 4096:
        raise ValueError("Upload details are too long")
    if "|" in args:
        parts = [part.strip() for part in args.split("|")]
        if len(parts) == 4:  # Previous explicit-slug photo format.
            character_id, name, category, rarity_input = parts
        elif len(parts) == 3:
            name, category, rarity_input = parts
            character_id = generated_id
        else:
            raise ValueError("Use name | anime | rarity")
    else:
        try:
            parts = shlex.split(args)
        except ValueError:
            raise ValueError("Check the quotation marks") from None
        if len(parts) != 3:
            raise ValueError("Use two names and a rarity number")
        name, category, rarity_input = parts
        character_id = generated_id
        if not any(quote in args for quote in ('"', "'")):
            name = name.replace("_", " ").replace("-", " ")
            category = category.replace("_", " ").replace("-", " ")
    if not re.fullmatch(r"(?:[1-9]|1[0-8])", rarity_input):
        raise ValueError("Rarity must be a number from 1 to 18")
    rarity = parse_rarity(rarity_input)
    if rarity is None or rarity not in {entry.value for entry in RARITIES}:
        raise ValueError("Invalid rarity")
    basics = CharacterBasics(id=character_id, name=name, category=category, rarity=rarity)
    return UploadDetails(basics.id, basics.name, basics.category, basics.rarity)


def parse_replacement(args: str) -> str:
    """Require an exact immutable ID and the explicit media-replacement field."""
    parts = args.split()
    if (
        len(args) > 4096
        or len(parts) != 2
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", parts[0]) is None
        or parts[1] != "media"
    ):
        raise ValueError("Use /updatechar exact-id media")
    return parts[0]


def reply_media(message: Message) -> ReplyMedia:
    """Inspect a Telegram reply without downloading it or trusting file names."""
    if (
        message.ephemeral_message_id is not None
        or message.has_protected_content
        or message.has_media_spoiler
    ):
        raise InvalidArtwork("Protected, self-destructing or spoiler media cannot be published")
    item: Any
    kind: MediaKind
    if message.photo:
        item, kind = message.photo, "photo"
    elif message.animation:
        item, kind = message.animation, "animation"
    elif message.video:
        item, kind = message.video, "video"
    elif message.document and message.document.mime_type == "image/gif":
        item, kind = message.document, "animation"
    else:
        raise InvalidArtwork("Reply to a photo, video or GIF")
    if getattr(item, "ttl_seconds", None) is not None:
        raise InvalidArtwork("Self-destructing media cannot be published")
    size = getattr(item, "file_size", None)
    maximum = MAX_ARTWORK_BYTES if kind == "photo" else MAX_MEDIA_BYTES
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= maximum:
        raise InvalidArtwork("Photo limit is 3 MiB; video and GIF limit is 16 MiB")
    file_id, unique_id = getattr(item, "file_id", None), getattr(item, "file_unique_id", None)
    if not all(
        isinstance(value, str)
        and 1 <= len(value) <= 2048
        and value.isascii()
        and value.isprintable()
        for value in (file_id, unique_id)
    ):
        raise InvalidArtwork("Telegram media identity is unavailable")
    if not isinstance(file_id, str) or not isinstance(unique_id, str):
        raise InvalidArtwork("Telegram media identity is unavailable")
    return ReplyMedia(kind, file_id, unique_id, size)


def valid_upload_age(message: Message, *, now: datetime | None = None) -> bool:
    date = message.date
    if not isinstance(date, datetime):
        return False
    date = date.replace(tzinfo=UTC) if date.utcoffset() is None else date.astimezone(UTC)
    now = now or datetime.now(UTC)
    return now - UPLOAD_LIFETIME <= date <= now + timedelta(minutes=1)


def menu_token(
    key: bytes,
    bot_id: int,
    actor_id: int,
    message: Message,
    media: ReplyMedia,
    provider: Provider,
    *,
    purpose: UploadPurpose = "choose",
    revision: str | None = None,
) -> str:
    """Bind each distinct action to the bot, operator, command and source media."""
    text = str(message.text or message.caption or "")
    # Wzgram file IDs include Telegram's renewable file_reference; bind the
    # stable unique ID and declared size, not a reference that can refresh.
    fields = [
        str(bot_id),
        str(actor_id),
        str(message.chat.id),
        str(message.id),
        text,
        media.kind,
        media.unique_id,
        str(media.size),
        provider,
        purpose,
    ]
    if revision is not None:
        if purpose != "publish" or re.fullmatch(r"[0-9a-f]{64}", revision) is None:
            raise ValueError("Invalid catalog revision")
        fields.append(revision)
    material = "\0".join(fields).encode("utf-8")
    digest = hmac.new(key, material, hashlib.sha256).digest()[:12]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def valid_token(expected: str, actual: str) -> bool:
    return hmac.compare_digest(expected, actual)


async def claim_upload_action(limiter: RateLimiter, event_id: str) -> bool:
    """Make publish and cancel contend for one durable command-level claim."""
    return await limiter.claim_once(f"catalog-media:{event_id}", seconds=7 * 86400)


async def read_motion(
    client: Client, message: Message, media: ReplyMedia, slots: asyncio.Semaphore
) -> tuple[bytes, str]:
    if media.kind == "photo":
        raise InvalidArtwork("Expected video or animation")
    async with slots:
        payload = bytearray()
        async with asyncio.timeout(90):
            async for chunk in client.stream_media(
                message, limit=MAX_MEDIA_BYTES // (1024 * 1024) + 1
            ):
                if not isinstance(chunk, bytes) or len(chunk) > MAX_MEDIA_BYTES - len(payload):
                    raise InvalidArtwork("Video or GIF is too large")
                payload.extend(chunk)
        if len(payload) != media.size:
            raise InvalidArtwork("Telegram did not provide complete media")
    data = bytes(payload)
    if valid_media_signature(data, "gif"):
        extension = "gif"
    elif valid_media_signature(data, "mp4"):
        extension = (
            "mov" if getattr(message.video, "mime_type", None) == "video/quicktime" else "mp4"
        )
    elif valid_media_signature(data, "webm"):
        extension = "webm"
    else:
        raise InvalidArtwork("Supported moving media: GIF, MP4, MOV and WebM")
    if media.kind == "video" and extension == "gif":
        raise InvalidArtwork("A video must contain video media")
    return data, extension
