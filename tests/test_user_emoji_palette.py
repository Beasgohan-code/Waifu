"""User-supplied custom emoji IDs remain strings and retain Unicode fallbacks."""

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from summon.bot.emoji_palette import VECTOR_ICONS, vector_icon
from summon.bot.presentation import Card, Emoji, fallback_keyboard


@pytest.mark.parametrize(
    ("number", "glyph", "emoji_id"),
    [
        (1, "✅", "5219899949281453881"),
        (3, "✨", "5222108309795908493"),
        (12, "🚫", "5219805369806629055"),
        (14, "❗", "5220197908342648622"),
        (15, "❓", "5220053623211305785"),
        (16, "🔝", "5246794802560774143"),
        (20, "🔧", "5258023599419171861"),
        (25, "✅", "5276489300207217985"),
        (34, "💎", "5264892613630111886"),
        (37, "⭐", "5310224206732996002"),
        (38, "🌙", "5377377257356537351"),
        (43, "✅", "5465542769755826716"),
        (48, "🔄", "5292226786229236118"),
        (53, "🔒", "5393302369024882368"),
        (56, "🪙", "5237761614458933049"),
        (62, "⌛", "5258113901106580375"),
        (64, "🔍", "5258274739041883702"),
        (71, "💱", "5258296359907249075"),
        (82, "🪙", "5283232570660634549"),
        (100, "📞", "5453965363286925977"),
        (102, "❤️", "5454249887690415056"),
        (116, "🐈‍⬛", "5453976908159016299"),
        (118, "🚩", "5454310635707853545"),
    ],
)
def test_palette_preserves_user_supplied_glyph_and_exact_string_id(number, glyph, emoji_id):
    assert vector_icon(number) == (glyph, emoji_id)


def test_palette_preserves_all_entries_and_rejects_bad_indexes():
    assert len(VECTOR_ICONS) == 118
    assert all(
        glyph and len(identifier) == 19 and identifier.isascii() and identifier.isdigit()
        for glyph, identifier in VECTOR_ICONS
    )
    assert len({identifier for _, identifier in VECTOR_ICONS}) == 118
    for number in (0, -1, 119):
        with pytest.raises(IndexError):
            vector_icon(number)


@pytest.mark.parametrize(
    ("role", "number"),
    [
        ("SPARK", 3),
        ("STAR", 37),
        ("WARNING", 14),
        ("CHECK", 1),
        ("SETTINGS", 20),
        ("COIN", 56),
        ("HOURGLASS", 62),
        ("HEART", 102),
        ("CANCEL", 12),
        ("DIAMOND", 34),
        ("TOP", 16),
        ("MOON", 38),
    ],
)
def test_semantic_emoji_renders_the_user_id_and_readable_unicode(role, number):
    emoji = Emoji[role]
    glyph, identifier = vector_icon(number)
    assert emoji.value == (glyph, identifier)
    card = Card("Example <safe>", emoji=emoji)
    assert f'<tg-emoji emoji-id="{identifier}">{glyph}</tg-emoji>' in card.telegram_html()
    assert f'<tg-emoji emoji-id="{identifier}">{glyph}</tg-emoji>' in card.rich_html()
    plain = card.telegram_html(premium=False)
    assert glyph in plain and "tg-emoji" not in plain and "&lt;safe&gt;" in plain


def test_every_user_supplied_button_icon_has_a_unicode_fallback_and_keeps_its_action():
    for glyph, identifier in VECTOR_ICONS:
        original = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Open", callback_data="safe:action", icon_custom_emoji_id=identifier
                    )
                ]
            ]
        )
        restored = fallback_keyboard(original).inline_keyboard[0][0]
        assert restored.text == f"{glyph} Open"
        assert restored.icon_custom_emoji_id is None
        assert restored.callback_data == "safe:action"
        assert original.inline_keyboard[0][0].icon_custom_emoji_id == identifier
