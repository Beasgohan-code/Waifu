"""Reusable Telegram keyboards with supplied emoji and Unicode fallbacks."""

from __future__ import annotations

from typing import Literal

from wzgram.enums import ButtonStyle
from wzgram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from summon.bot.presentation import Emoji, button_label


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
