"""Reusable Telegram keyboards with supplied emoji and Unicode fallbacks."""

from __future__ import annotations

import re
from typing import Literal

from wzgram.enums import ButtonStyle
from wzgram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from summon.bot.presentation import Emoji, button_label
from summon.models import CollectionMode


def collection_keyboard(url: str, *, premium: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=button_label(
                        "Open the collection", Emoji.OPEN_COLLECTION, premium=premium
                    ),
                    web_app=WebAppInfo(url=url),
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id=Emoji.OPEN_COLLECTION.custom_id if premium else None,
                )
            ]
        ]
    )


def start_keyboard(
    webapp_url: str, bot_username: str | None, actor_id: int, *, premium: bool
) -> InlineKeyboardMarkup:
    rows = collection_keyboard(webapp_url, premium=premium).inline_keyboard if webapp_url else []
    if bot_username and re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot_username):
        rows.append(
            [
                InlineKeyboardButton(
                    text=button_label("Add to a group", Emoji.SPARK, premium=premium),
                    url=f"https://t.me/{bot_username}?startgroup=true",
                    icon_custom_emoji_id=Emoji.SPARK.custom_id if premium else None,
                    style=ButtonStyle.PRIMARY,
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=button_label("How to play", Emoji.QUESTION, premium=premium),
                callback_data=f"start-guide:{actor_id}",
                icon_custom_emoji_id=Emoji.QUESTION.custom_id if premium else None,
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def upload_provider_keyboard(
    choices: list[tuple[str, str]], *, premium: bool
) -> InlineKeyboardMarkup:
    """Provider callbacks are signed and actor-bound by the upload handler."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=button_label(label, Emoji.OPEN_COLLECTION, premium=premium),
                    callback_data=data,
                    icon_custom_emoji_id=Emoji.OPEN_COLLECTION.custom_id if premium else None,
                    style=ButtonStyle.PRIMARY,
                )
                for label, data in choices
            ]
        ]
    )


def operator_panel_keyboard(url: str, *, premium: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=button_label("Open the Mini App", Emoji.ADMIN_SHIELD, premium=premium),
                    web_app=WebAppInfo(url=url),
                    style=ButtonStyle.PRIMARY,
                    icon_custom_emoji_id=Emoji.ADMIN_SHIELD.custom_id if premium else None,
                )
            ]
        ]
    )


def collection_mode_keyboard(
    actor_id: int, selected: CollectionMode, *, premium: bool
) -> InlineKeyboardMarkup:
    modes: tuple[tuple[CollectionMode, str, Emoji], ...] = (
        ("recent", "Recent", Emoji.HOURGLASS),
        ("copies", "Copies", Emoji.OPEN_COLLECTION),
        ("favorites", "Favorites", Emoji.HEART),
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=button_label(
                        f"{'✓ ' if selected == mode else ''}{label}", icon, premium=premium
                    ),
                    callback_data=f"hmode:{actor_id}:{mode}",
                    icon_custom_emoji_id=icon.custom_id if premium else None,
                    style=ButtonStyle.PRIMARY if selected == mode else ButtonStyle.DEFAULT,
                )
                for mode, label, icon in modes
            ]
        ]
    )


def exchange_keyboard(
    kind: Literal["trade", "gift"], offer_id: str, *, premium: bool
) -> InlineKeyboardMarkup:
    icon = Emoji.CHECK if kind == "trade" else Emoji.GIFT_ACTION
    text = "Accept" if kind == "trade" else "Accept gift"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=button_label(text, icon, premium=premium),
                    callback_data=f"{kind}:accept:{offer_id}",
                    style=ButtonStyle.SUCCESS,
                    icon_custom_emoji_id=icon.custom_id if premium else None,
                ),
                InlineKeyboardButton(
                    text=button_label("Cancel", Emoji.CANCEL, premium=premium),
                    callback_data=f"{kind}:cancel:{offer_id}",
                    style=ButtonStyle.DANGER,
                    icon_custom_emoji_id=Emoji.CANCEL.custom_id if premium else None,
                ),
            ]
        ]
    )
