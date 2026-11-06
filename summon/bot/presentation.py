"""Shared bot presentation with safe Richgram and premium-emoji fallbacks.

The active palette uses selected IDs from the user's supplied lists; the full
numbered lists and attribution are retained separately. Telegram eligibility,
pack availability, and rights still apply; rendering is not live-verified.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import InputRichMessage as BotApiRichMessage
from wzgram.errors import BadRequest
from wzgram.types import InputRichMessage

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

    def render(self, *, premium: bool = True) -> str:
        glyph, emoji_id = self.value
        if not premium:
            return glyph
        return f'<tg-emoji emoji-id="{emoji_id}">{glyph}</tg-emoji>'


@dataclass(frozen=True, slots=True)
class Card:
    title: str
    body: str = ""
    emoji: Emoji = Emoji.SPARK
    fields: tuple[tuple[str, str], ...] = ()
    details: tuple[str, str] | None = None

    def telegram_html(self, *, premium: bool = True) -> str:
        heading = f"{self.emoji.render(premium=premium)} <b>{html.escape(self.title)}</b>"
        parts = [heading]
        if self.body:
            parts.append(html.escape(self.body))
        if self.fields:
            parts.append(
                "\n".join(f"<b>{html.escape(k)}</b>  {html.escape(v)}" for k, v in self.fields)
            )
        if self.details:
            parts.append(f"<b>{html.escape(self.details[0])}</b>\n{html.escape(self.details[1])}")
        return "\n\n".join(parts)

    def rich_html(self, *, premium: bool = True) -> str:
        heading = f"{self.emoji.render(premium=premium)} {html.escape(self.title)}"
        pieces = [richgram.rich_heading(heading, level=3)]
        if self.body:
            pieces.append(f"<p>{html.escape(self.body)}</p>")
        if self.fields:
            pairs = [(html.escape(k), html.escape(v)) for k, v in self.fields]
            pieces.append(richgram.rich_kv_table(pairs, compact=True))
        if self.details:
            pieces.append(
                richgram.rich_details(html.escape(self.details[0]), html.escape(self.details[1]))
            )
        return "".join(pieces)


def _emoji_rejected(exc: TelegramBadRequest) -> bool:
    message = str(exc).casefold()
    return (
        "custom_emoji" in message or "custom emoji" in message or "can't parse entities" in message
    )


def button_label(text: str, emoji: Emoji, *, premium: bool) -> str:
    """Show the corresponding Unicode glyph when premium button icons are off."""
    return text if premium else f"{emoji.value[0]} {text}"


def fallback_keyboard(markup: Any) -> Any:
    """Strip visual extras without mutating the original or dropping actions."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    payload = markup.model_dump(exclude_none=True)
    glyphs = dict(PALETTE_FALLBACKS)
    glyphs.update({entry.value[1]: entry.value[0] for entry in Emoji})
    for row in payload["inline_keyboard"]:
        for button in row:
            icon = button.pop("icon_custom_emoji_id", None)
            button.pop("style", None)
            if icon in glyphs:
                button["text"] = f"{glyphs[icon]} {button['text']}"
    return InlineKeyboardMarkup.model_validate(payload)


async def send_card(
    bot: Any,
    chat_id: int,
    card: Card,
    *,
    rich_client: Any | None = None,
    bot_api_rich: bool = False,
    premium: bool = True,
    reply_markup: Any | None = None,
    message_thread_id: int | None = None,
) -> Any:
    """Send exactly once unless Telegram definitively rejects a visual feature.

    Richgram's builders are used, but its stock sender retries all exceptions,
    including ambiguous network timeouts. We deliberately do not do that. Native
    keyboards stay on the Bot API path so callbacks are never lost in fallback.
    """
    if rich_client is not None and richgram.RICH_AVAILABLE and reply_markup is None:
        try:
            return await rich_client.send_rich_message(
                chat_id=chat_id,
                rich_text=InputRichMessage(html=card.rich_html(premium=premium)),
                message_thread_id=message_thread_id,
            )
        except BadRequest:
            logger.info("Wzgram rich message rejected; retrying as a standard message")
    elif bot_api_rich:
        # The Bot API accepts aiogram keyboards directly, including callback data.
        # A timeout is ambiguous and must not trigger a second delivery attempt.
        rich_kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": BotApiRichMessage(html=card.rich_html(premium=premium)),
        }
        if reply_markup is not None:
            rich_kwargs["reply_markup"] = reply_markup
        if message_thread_id is not None:
            rich_kwargs["message_thread_id"] = message_thread_id
        try:
            return await bot.send_rich_message(**rich_kwargs)
        except TelegramBadRequest:
            logger.info("Bot API rich message rejected; using readable standard text")
            premium = False
            reply_markup = fallback_keyboard(reply_markup)

    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "text": card.telegram_html(premium=premium),
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    if message_thread_id is not None:
        kwargs["message_thread_id"] = message_thread_id
    try:
        return await bot.send_message(**kwargs)
    except TelegramBadRequest as exc:
        if not premium or not _emoji_rejected(exc):
            raise
        logger.info("Custom emoji rejected; retrying with Unicode")
        kwargs["text"] = card.telegram_html(premium=False)
        if reply_markup is not None:
            kwargs["reply_markup"] = fallback_keyboard(reply_markup)
        return await bot.send_message(**kwargs)


async def send_photo_card(
    bot: Any,
    chat_id: int,
    photo: str,
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
        "parse_mode": "HTML",
        "message_thread_id": message_thread_id,
    }
    try:
        return await bot.send_photo(**kwargs)
    except TelegramBadRequest as exc:
        if not premium or not _emoji_rejected(exc):
            raise
        logger.info("Custom emoji in photo caption rejected; retrying with Unicode")
        kwargs["caption"] = card.telegram_html(premium=False)
        return await bot.send_photo(**kwargs)


async def send_ephemeral_card(
    bot: Any,
    group_id: int,
    user_id: int,
    card: Card,
    *,
    premium: bool = True,
    message_thread_id: int | None = None,
    incoming_ephemeral_id: int | None = None,
) -> Any:
    """Try a 10.3 private overlay; on definite rejection send a DM, never public content."""
    from aiogram.types import EphemeralMessageParameters, ReplyParameters

    kwargs: dict[str, Any] = {
        "chat_id": group_id,
        "text": card.telegram_html(premium=premium),
        "parse_mode": "HTML",
        "ephemeral_message_parameters": EphemeralMessageParameters(receiver_user_id=user_id),
    }
    if message_thread_id is not None:
        kwargs["message_thread_id"] = message_thread_id
    if incoming_ephemeral_id is not None:
        kwargs["reply_parameters"] = ReplyParameters(ephemeral_message_id=incoming_ephemeral_id)
    try:
        return await bot.send_message(**kwargs)
    except TelegramBadRequest as exc:
        if premium and _emoji_rejected(exc):
            kwargs["text"] = card.telegram_html(premium=False)
            try:
                return await bot.send_message(**kwargs)
            except TelegramBadRequest:
                logger.info("Ephemeral message rejected; attempting private chat")
        else:
            logger.info("Ephemeral message rejected; attempting private chat")
    return await send_card(bot, user_id, card, premium=premium)
