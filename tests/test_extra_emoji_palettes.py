"""All extra user-provided packs are addressable without losing 64-bit IDs."""

import pytest
from wzgram.types import InlineKeyboardButton, InlineKeyboardMarkup

from summon.bot.emoji_extra_palettes import _parse_palette
from summon.bot.emoji_palette import PALETTES, palette_icon
from summon.bot.presentation import Card, Emoji, fallback_keyboard


def test_all_seven_user_palettes_are_preserved_and_have_distinct_ids():
    assert {name: len(icons) for name, icons in PALETTES.items()} == {
        "vector": 118,
        "emoji_fan": 200,
        "rijif": 200,
        "boseh": 200,
        "news": 100,
        "summon_bot": 60,
        "telegram_ios": 100,
    }
    all_identifiers = [identifier for icons in PALETTES.values() for _, identifier in icons]
    assert len(all_identifiers) == len(set(all_identifiers)) == 978
    for icons in PALETTES.values():
        assert len({identifier for _, identifier in icons}) == len(icons)
        assert all(
            glyph and len(identifier) == 19 and identifier.isascii() and identifier.isdigit()
            for glyph, identifier in icons
        )


@pytest.mark.parametrize(
    ("palette", "number", "expected"),
    [
        ("emoji_fan", 1, ("❤️‍🔥", "4956222745814762495")),
        ("emoji_fan", 5, ("🎁", "4958699241137505132")),
        ("emoji_fan", 27, ("🛡", "4958900559139570572")),
        ("emoji_fan", 35, ("🐦‍⬛️", "4958830950604604428")),
        ("emoji_fan", 92, ("📊", "4958506272551863292")),
        ("emoji_fan", 125, ("⭐️", "4956591756519932897")),
        ("emoji_fan", 200, ("🌹", "4958597497657230624")),
        ("rijif", 1, ("❤️‍🔥", "5042225965518816316")),
        ("rijif", 31, ("👑", "5039727497143387500")),
        ("rijif", 86, ("🔄", "5041837837914211014")),
        ("rijif", 125, ("⭐", "5042061201983407048")),
        ("rijif", 200, ("🌹", "5039850573726221609")),
        ("boseh", 1, ("✅", "5084979757905347540")),
        ("boseh", 45, ("❓", "5116240346656801621")),
        ("boseh", 99, ("🧪", "5134183530313548836")),
        ("boseh", 113, ("0️⃣", "5140999334174655345")),
        ("boseh", 119, ("6️⃣", "5139005588881015916")),
        ("boseh", 122, ("9️⃣", "5141137309999039199")),
        ("boseh", 141, ("⚙️", "4904936030232117798")),
        ("boseh", 200, ("📍", "5104989658350093220")),
        ("news", 1, ("👀", "5210956306952758910")),
        ("news", 18, ("📊", "5231200819986047254")),
        ("news", 51, ("🔍", "5231012545799666522")),
        ("news", 52, ("🛡", "5251203410396458957")),
        ("news", 69, ("✨", "5325547803936572038")),
        ("news", 100, ("🎉", "5461151367559141950")),
    ],
)
def test_original_numbered_entries_match_user_input(palette, number, expected):
    assert palette_icon(palette, number) == expected


def test_pack_lookup_rejects_unknown_or_out_of_range_values():
    with pytest.raises(KeyError):
        palette_icon("unknown", 1)
    for name, icons in PALETTES.items():
        for number in (0, -1, len(icons) + 1):
            with pytest.raises(IndexError):
                palette_icon(name, number)


def test_palette_registry_is_immutable():
    with pytest.raises(TypeError):
        PALETTES["new"] = ()
    assert all(isinstance(icons, tuple) for icons in PALETTES.values())


@pytest.mark.parametrize(
    "bad_source",
    [
        "2)✨ [5222108309795908493]",  # starts at the wrong index
        "1)✨ [522210830979590849]",  # ID is too short
        "1)✨ [٥٢٢٢١٠٨٣٠٩٧٩٥٩٠٨٤٩٣]",  # IDs must use ASCII digits
        "1)✨ [5222108309795908493]\n3)⭐ [5310224206732996002]",  # missing index
    ],
)
def test_extra_palette_parser_rejects_invalid_numbering_or_ids(bad_source):
    with pytest.raises(ValueError):
        _parse_palette(bad_source, expected_count=len(bad_source.splitlines()))


@pytest.mark.parametrize(
    ("role", "palette", "number"),
    [
        ("GIFT", "emoji_fan", 5),
        ("STATS", "emoji_fan", 92),
        ("CROWN", "rijif", 31),
        ("TRADE", "rijif", 86),
        ("GEAR", "boseh", 141),
        ("QUESTION", "boseh", 45),
        ("SHIELD", "news", 52),
        ("SEARCH", "news", 51),
    ],
)
def test_real_message_roles_use_each_new_pack_with_unicode_fallback(role, palette, number):
    emoji = Emoji[role]
    glyph, identifier = palette_icon(palette, number)
    assert emoji.value == (glyph, identifier)
    message = Card("Ready", emoji=emoji)
    assert f'<tg-emoji emoji-id="{identifier}">{glyph}</tg-emoji>' in message.telegram_html()
    assert f'<tg-emoji emoji-id="{identifier}">{glyph}</tg-emoji>' in message.rich_html()
    assert glyph in message.telegram_html(premium=False)
    assert "tg-emoji" not in message.telegram_html(premium=False)
    assert "tg-emoji" not in message.rich_html(premium=False)


@pytest.mark.parametrize(
    "palette", ["vector", "emoji_fan", "rijif", "boseh", "news", "summon_bot", "telegram_ios"]
)
async def test_button_serializes_each_palette_id_as_exact_mtproto_int(palette):

    _, identifier = palette_icon(palette, 1)
    button = InlineKeyboardButton(
        text="Open", callback_data="safe:action", icon_custom_emoji_id=identifier
    )
    payload = await button.write(None)
    assert button.icon_custom_emoji_id == identifier
    assert payload.style.icon == int(identifier)


def test_every_pack_button_icon_has_a_safe_fallback_without_changing_actions():
    for icons in PALETTES.values():
        for glyph, identifier in icons:
            original = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Open",
                            callback_data="safe:action",
                            icon_custom_emoji_id=identifier,
                        )
                    ]
                ]
            )
            button = fallback_keyboard(original).inline_keyboard[0][0]
            assert button.text == f"{glyph} Open"
            assert button.icon_custom_emoji_id is None
            assert button.callback_data == "safe:action"
            assert original.inline_keyboard[0][0].icon_custom_emoji_id == identifier
