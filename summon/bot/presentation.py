"""Shared bot presentation with safe Richgram and premium-emoji fallbacks.

The active palette uses selected IDs from the user's supplied lists; the full
numbered lists and attribution are retained separately. Telegram eligibility,
pack availability, and rights still apply; rendering is not live-verified.
"""

from __future__ import annotations

import copy
import html
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, BinaryIO

from wzgram.enums import ButtonStyle, ParseMode
from wzgram.errors import BadRequest
from wzgram.types import (
    EphemeralMessageParameters,
    InlineKeyboardMarkup,
    InputRichMessage,
    ReplyParameters,
)

from summon._vendor import richgram_builders as richgram
from summon.bot.emoji_palette import PALETTE_FALLBACKS, palette_icon, vector_icon

logger = logging.getLogger(__name__)


class Emoji(Enum):
    # Select semantic roles from the user's numbered palettes.
    SPARK = vector_icon(3)
    STAR = vector_icon(37)
    GIFT = palette_icon("emoji_fan", 5)
    SHIELD = palette_icon("news", 52)
    WARNING = vector_icon(14)
    CHECK = vector_icon(1)
    SEARCH = palette_icon("news", 51)
    STATS = palette_icon("emoji_fan", 92)
    SETTINGS = vector_icon(20)
    CROWN = palette_icon("rijif", 31)
    GEAR = palette_icon("boseh", 141)
    COIN = vector_icon(56)
    TRADE = palette_icon("rijif", 86)
    HOURGLASS = vector_icon(62)
    HEART = vector_icon(102)
    CANCEL = vector_icon(12)
    DIAMOND = vector_icon(34)
    QUESTION = palette_icon("boseh", 45)
    TOP = vector_icon(16)
    MOON = vector_icon(38)
    # New user-supplied SUMMON bot and Telegram iOS sets. Keep existing roles
    # available as well so earlier palettes are not displaced wholesale.
    SUMMON_BRAND = palette_icon("summon_bot", 34)
    OPEN_COLLECTION = palette_icon("summon_bot", 60)
    SERIES_TAG = palette_icon("summon_bot", 1)
    RARITY_STAR = palette_icon("summon_bot", 5)
    REWARD_COIN = palette_icon("summon_bot", 48)
    GIFT_ACTION = palette_icon("summon_bot", 19)
    DONE = palette_icon("summon_bot", 37)
    ALERT = palette_icon("summon_bot", 38)
    NOTE = palette_icon("telegram_ios", 22)
    NOTE_EDIT = palette_icon("telegram_ios", 30)
    NOTE_DELETE = palette_icon("telegram_ios", 77)
    RULES = palette_icon("telegram_ios", 76)
    PREVIOUS_PAGE = palette_icon("telegram_ios", 47)
    NEXT_PAGE = palette_icon("telegram_ios", 48)
    PRIVATE = palette_icon("telegram_ios", 68)
    ADMIN_SHIELD = palette_icon("telegram_ios", 94)
    SNOW = palette_icon("summon_bot", 8)
    RAIN = palette_icon("summon_bot", 58)
    HALLOWEEN = palette_icon("summon_bot", 45)
    CHRISTMAS = palette_icon("summon_bot", 17)
    CELEBRATE = palette_icon("summon_bot", 55)
    MUSIC = vector_icon(2)

    @property
    def custom_id(self) -> str | None:
        value = self.value[1]
        if re.fullmatch(r"[1-9][0-9]{0,18}", value) and int(value) <= 2**63 - 1:
            return value
        return None

    def render(self, *, premium: bool = True) -> str:
        glyph = html.escape(self.value[0], quote=False)
        emoji_id = self.custom_id
        if not premium or emoji_id is None:
            return glyph
        return f'<tg-emoji emoji-id="{emoji_id}">{glyph}</tg-emoji>'


_RARITY_ICONS = {
    "common": Emoji.RARITY_STAR,
    "rare": Emoji.STAR,
    "special edition": Emoji.SUMMON_BRAND,
    "legendary": Emoji.CROWN,
    "mythic edition": Emoji.DIAMOND,
    "valentine edition": Emoji.HEART,
    "summer edition": Emoji.SPARK,
    "rainy edition": Emoji.RAIN,
    "halloween edition": Emoji.HALLOWEEN,
    "christmas edition": Emoji.CHRISTMAS,
    "winter edition": Emoji.SNOW,
    "new year edition": Emoji.CELEBRATE,
    "festival edition": Emoji.CELEBRATE,
    "amv edition": Emoji.MUSIC,
    "event edition": Emoji.GIFT_ACTION,
    "celestial edition": Emoji.STAR,
    "luxury edition": Emoji.DIAMOND,
    "limited edition": Emoji.HOURGLASS,
    "epic (legacy)": Emoji.RARITY_STAR,
}


def _field_icon(key: str, value: str) -> Emoji:
    """Decorate authored labels, not arbitrary user-supplied message bodies."""
    label = key.casefold().strip()
    if label == "rarity":
        return _RARITY_ICONS.get(value.casefold().strip(), Emoji.RARITY_STAR)
    if label in _RARITY_ICONS:
        return _RARITY_ICONS[label]
    if any(word in label for word in ("series", "category")):
        return Emoji.SERIES_TAG
    if any(word in label for word in ("point", "balance", "reward", "coin")):
        return Emoji.REWARD_COIN
    if any(word in label for word in ("trade", "gift")):
        return Emoji.GIFT_ACTION
    if any(word in label for word in ("group", "admin", "moderation", "security")):
        return Emoji.ADMIN_SHIELD
    if any(word in label for word in ("time", "daily", "streak", "expires")):
        return Emoji.HOURGLASS
    if any(word in label for word in ("owned", "collection", "copies", "favorite")):
        return Emoji.OPEN_COLLECTION
    if any(word in label for word in ("progress", "level", "xp", "experience")):
        return Emoji.STAR
    return Emoji.SPARK


@dataclass(frozen=True, slots=True)
class Card:
    title: str
    body: str = ""
    emoji: Emoji = Emoji.SPARK
    fields: tuple[tuple[str, str], ...] = ()
    details: tuple[str, str] | None = None
    field_icons: tuple[Emoji | None, ...] = ()

    def __post_init__(self) -> None:
        if self.field_icons and len(self.field_icons) != len(self.fields):
            raise ValueError("Field icons must match the number of fields")

    def _render_fields(self, *, premium: bool) -> list[tuple[str, str]]:
        rendered = []
        for index, (key, value) in enumerate(self.fields):
            explicit = self.field_icons[index] if self.field_icons else None
            icon = explicit or _field_icon(key, value)
            rendered.append(
                (f"{icon.render(premium=premium)} {html.escape(key)}", html.escape(value))
            )
        return rendered

    def telegram_html(self, *, premium: bool = True) -> str:
        heading = f"{self.emoji.render(premium=premium)} <b>{html.escape(self.title)}</b>"
        parts = [heading]
        if self.body:
            parts.append(html.escape(self.body))
        if self.fields:
            parts.append(
                "\n".join(
                    f"<b>{key}</b>  {value}" for key, value in self._render_fields(premium=premium)
                )
            )
        if self.details:
            label = f"{Emoji.NOTE.render(premium=premium)} {html.escape(self.details[0])}"
            parts.append(f"<b>{label}</b>\n{html.escape(self.details[1])}")
        return "\n\n".join(parts)

    def rich_html(self, *, premium: bool = True) -> str:
        heading = f"{self.emoji.render(premium=premium)} {html.escape(self.title)}"
        pieces = [richgram.rich_heading(heading, level=3)]
        if self.body:
            pieces.append(f"<p>{html.escape(self.body)}</p>")
        if self.fields:
            pieces.append(
                richgram.rich_kv_table(self._render_fields(premium=premium), compact=True)
            )
        if self.details:
            pieces.append(
                richgram.rich_details(
                    f"{Emoji.NOTE.render(premium=premium)} {html.escape(self.details[0])}",
                    html.escape(self.details[1]),
                )
            )
        return "".join(pieces)


def _emoji_rejected(exc: BadRequest) -> bool:
    message = str(exc).casefold()
    return (
        "custom_emoji" in message or "custom emoji" in message or "can't parse entities" in message
    )


def button_label(text: str, emoji: Emoji, *, premium: bool) -> str:
    """Show the corresponding Unicode glyph when premium button icons are off."""
    return text if premium and emoji.custom_id else f"{emoji.value[0]} {text}"


def callback_text(text: str, *, emoji: Emoji) -> str:
    """Callback toasts have plain text only; use Unicode, never Telegram HTML."""
    return f"{emoji.value[0]} {text}"[:200]


def fallback_keyboard(markup: Any) -> Any:
    """Remove visual extras without mutating the Wzgram keyboard or its actions."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    glyphs = dict(PALETTE_FALLBACKS)
    glyphs.update({entry.value[1]: entry.value[0] for entry in Emoji})
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for button in row:
            clone = copy.copy(button)
            icon = clone.icon_custom_emoji_id
            clone.icon_custom_emoji_id = None
            clone.style = ButtonStyle.DEFAULT
            if icon in glyphs:
                clone.text = f"{glyphs[icon]} {clone.text}"
            new_row.append(clone)
        rows.append(new_row)
    return InlineKeyboardMarkup(rows, force_reply=markup.force_reply)


async def send_card(
    client: Any,
    chat_id: int,
    card: Card,
    *,
    rich: bool = True,
    premium: bool = True,
    reply_markup: Any | None = None,
    message_thread_id: int | None = None,
) -> Any:
    """Use Wzgram natively; retry only after an unambiguous visual rejection."""
    if rich and richgram.RICH_AVAILABLE:
        try:
            return await client.send_rich_message(
                chat_id=chat_id,
                rich_text=InputRichMessage(html=card.rich_html(premium=premium)),
                reply_markup=reply_markup,
                message_thread_id=message_thread_id,
            )
        except BadRequest as exc:
            logger.info("Rich message rejected; using readable standard text")
            # An unsupported rich layout does not imply custom emoji are
            # unavailable in ordinary Telegram messages or native buttons.
            if _emoji_rejected(exc):
                premium = False
                reply_markup = fallback_keyboard(reply_markup)
            elif not premium:
                reply_markup = fallback_keyboard(reply_markup)
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "text": card.telegram_html(premium=premium),
        "parse_mode": ParseMode.HTML,
        "reply_markup": reply_markup,
        "message_thread_id": message_thread_id,
    }
    try:
        return await client.send_message(**kwargs)
    except BadRequest as exc:
        if not premium or not _emoji_rejected(exc):
            raise
        logger.info("Custom emoji rejected; retrying with Unicode")
        kwargs["text"] = card.telegram_html(premium=False)
        kwargs["reply_markup"] = fallback_keyboard(reply_markup)
        return await client.send_message(**kwargs)


async def edit_text_card(
    message: Any, card: Card, *, premium: bool, reply_markup: Any | None = None
) -> Any:
    """Edit ordinary text, falling back only after a definite emoji rejection."""
    try:
        return await message.edit_text(
            text=card.telegram_html(premium=premium),
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except BadRequest as exc:
        if not premium or not _emoji_rejected(exc):
            raise
        return await message.edit_text(
            text=card.telegram_html(premium=False),
            parse_mode=ParseMode.HTML,
            reply_markup=fallback_keyboard(reply_markup),
        )


async def edit_keyboard(message: Any, markup: Any, *, premium: bool) -> Any:
    """Retry an edited keyboard only after a definite custom-emoji rejection."""
    try:
        return await message.edit_reply_markup(reply_markup=markup)
    except BadRequest as exc:
        if not premium or not _emoji_rejected(exc):
            raise
        return await message.edit_reply_markup(reply_markup=fallback_keyboard(markup))


async def send_photo_card(
    client: Any,
    chat_id: int,
    photo: str | BinaryIO,
    card: Card,
    *,
    premium: bool = True,
    message_thread_id: int | None = None,
) -> Any:
    """Preserve the artwork while falling back after a definite emoji rejection."""
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "photo": photo,
        "caption": card.telegram_html(premium=premium),
        "parse_mode": ParseMode.HTML,
        "message_thread_id": message_thread_id,
    }
    try:
        return await client.send_photo(**kwargs)
    except BadRequest as exc:
        if not premium or not _emoji_rejected(exc):
            raise
        logger.info("Custom emoji in photo caption rejected; retrying with Unicode")
        kwargs["caption"] = card.telegram_html(premium=False)
        if hasattr(photo, "seek"):
            photo.seek(0)
        return await client.send_photo(**kwargs)


async def send_media_card(
    client: Any,
    chat_id: int,
    media: str | BinaryIO,
    card: Card,
    *,
    media_type: str,
    premium: bool = True,
    message_thread_id: int | None = None,
) -> Any:
    """Send photo, video, or animation; never retry an ambiguous delivery."""
    if media_type == "photo":
        return await send_photo_card(
            client, chat_id, media, card, premium=premium, message_thread_id=message_thread_id
        )
    if media_type not in {"video", "animation"}:
        raise ValueError("Unsupported media type")
    sender = client.send_video if media_type == "video" else client.send_animation
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        media_type: media,
        "caption": card.telegram_html(premium=premium),
        "parse_mode": ParseMode.HTML,
        "message_thread_id": message_thread_id,
    }
    try:
        return await sender(**kwargs)
    except BadRequest as exc:
        if not premium or not _emoji_rejected(exc):
            raise
        logger.info("Custom emoji in media caption rejected; retrying with Unicode")
        kwargs["caption"] = card.telegram_html(premium=False)
        if hasattr(media, "seek"):
            media.seek(0)
        return await sender(**kwargs)


async def send_ephemeral_card(
    client: Any,
    group_id: int,
    user_id: int,
    card: Card,
    *,
    premium: bool = True,
    message_thread_id: int | None = None,
    incoming_ephemeral_id: int | None = None,
) -> Any:
    """Try a private overlay; on definite rejection use a DM, never public content."""
    kwargs: dict[str, Any] = {
        "chat_id": group_id,
        "text": card.telegram_html(premium=premium),
        "parse_mode": ParseMode.HTML,
        "ephemeral_message_parameters": EphemeralMessageParameters(receiver_user_id=user_id),
        "message_thread_id": message_thread_id,
    }
    if incoming_ephemeral_id is not None:
        kwargs["reply_parameters"] = ReplyParameters(ephemeral_message_id=incoming_ephemeral_id)
    try:
        return await client.send_message(**kwargs)
    except BadRequest as exc:
        if premium and _emoji_rejected(exc):
            kwargs["text"] = card.telegram_html(premium=False)
            try:
                return await client.send_message(**kwargs)
            except BadRequest:
                logger.info("Private overlay rejected; attempting private chat")
        else:
            logger.info("Private overlay rejected; attempting private chat")
    return await send_card(client, user_id, card, rich=False, premium=premium)
