"""Regression tests for the user's numbered SUMMON and Telegram iOS icon lists."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from summon.bot.emoji_palette import PALETTES, palette_icon
from summon.bot.presentation import Card, Emoji, fallback_keyboard, send_card

# Complete numbered pairs transcribed from the user's message. Production data
# was generated from this fixture, so this detects drift, not transcription errors.
SUMMON_GOLDEN = """
1)🏷 [6213102872964373588]
2)💰 [6212758407997300195]
3)⭐ [6212881347141181453]
4)🛡 [6212888532621467632]
5)⭐️ [6212872632652537745]
6)4️⃣ [6215084107018280981]
7)1️⃣ [6215384338117173007]
8)❄️ [6213147188436934178]
9)♨️ [6213236463627148940]
10)👻 [6215238098775711325]
11)✈️ [6215519058356346579]
12)⭐ [6213036047568215324]
13)⭐ [6213108619630616849]
14)☝️ [6212943680001548423]
15)💪 [6212968582221930176]
16)👑 [6213064845323936748]
17)🎄 [6213268830500693640]
18)📌 [6213185873207370614]
19)🎁 [6213142171915132223]
20)🥂 [6213050328334474612]
21)🌟 [6214971316882120764]
22)💔 [6215453006054301341]
23)🔻 [6213101004653600827]
24)🔹 [6213265381641952794]
25)🔸 [6213065064367267416]
26)🔹 [6215140259420708168]
27)🟫 [6213044143581569191]
28)🔸 [6212982356182049311]
29)🟢 [6212751553229496466]
30)⬜️ [6212972890074127433]
31)🌸 [6213270037386501707]
32)🌸 [6213064845323936747]
33)🔥 [6213087690254984403]
34)✨ [6215181302128189616]
35)🍬 [6212862878781808749]
36)👎 [6215355810944393322]
37)✅ [6212749607609310928]
38)🚨 [6213218197131238608]
39)🚫 [6215437965078831526]
40)🔴 [6214970556672909067]
41)🥰 [6215088724108124392]
42)🥳 [6212760113099317592]
43)🤯 [6213101112027784667]
44)👻 [6213097268032053230]
45)🎃 [6213078314341375822]
46)🎄 [6215161339120197379]
47)🗿 [6212892256358118824]
48)🪙 [6213059592578932964]
49)🔮 [6213160567260061243]
50)🎐 [6212940488840847051]
51)💌 [6212837985151361529]
52)💖 [6212880041471123172]
53)🌐 [6215009975882751628]
54)🚨 [6215075830616301088]
55)🎉 [6213198676504878714]
56)🔖 [6213221250852987064]
57)❄️ [6212718237168180631]
58)🌧 [6212824769536992332]
59)🔴 [6215331389760348345]
60)💎 [6215348101478097302]
"""

IOS_GOLDEN = """
1)✈️ [6028346797368283073]
2)🍏 [5775870512127283512]
3)⭐️ [6028338546736107668]
4)⭐️ [5767199127775481841]
5)🎁 [5773677501825945508]
6)🔣 [6035162669948867129]
7)🖥 [5942734685976138521]
8)⚙️ [5904258298764334001]
9)⚙ [6032742198179532882]
10)⚙️ [5850309953293653168]
11)⚙️ [5850332476102153487]
12)⚙️ [5850392884817172292]
13)⚙️ [5850224242926295392]
14)⚙️ [5924722061288150929]
15)🎛 [5776424837786374634]
16)🎛 [5771449289972650710]
17)📰 [5895519358871932592]
18)📰 [5893444447286334441]
19)📰 [5886437972647088483]
20)📰 [5893236738372932548]
21)📰 [5893057118545646106]
22)📝 [5920046907782074235]
23)📝 [5922693616953725714]
24)🗂 [5766994197705921104]
25)↔️ [5893316448670978477]
26)↔️ [5895534923833413814]
27)⬅️ [5960671702059848143]
28)🔁 [6030657343744644592]
29)⏫ [5938437708635443119]
30)✏️ [6039779802741739617]
31)✏ [6039614175917903752]
32)❌ [6030757850274336631]
33)⌨ [6039404727542747508]
34)📎 [6039451237743595514]
35)🔧 [5962952497197748583]
36)🔨 [5940433880585605708]
37)🚪 [6035130900075777681]
38)🔎 [6032850693348399258]
39)🏷 [5888620056551625531]
40)🏷 [5886285355279193209]
41)🏷 [5884050696679986441]
42)🏷 [5884090188904272855]
43)🏷 [5886473311637999700]
44)🏷 [5888880224195581055]
45)🏷 [5884491244360438851]
46)↩️ [5895507195524550741]
47)⬅️ [6039539366177541657]
48)➡️ [5895383238473421210]
49)➡️ [6037622221625626773]
50)📺 [6039391078136681499]
51)🔗 [6028171274939797252]
52)🔗 [6030864215139422409]
53)🔗 [5776078972659962594]
54)🔗 [5778455936410588193]
55)🔗 [5895364284782743985]
56)🔗 [5769289093221454192]
57)🔗 [5766902139376898645]
58)➕ [6032924188828767321]
59)ℹ [6028435952299413210]
60)❓ [6030848053177486888]
61)❗️ [6030563507299160824]
62)▶️ [5773626993010546707]
63)▶️ [5850346984501680054]
64)❌ [5774077015388852135]
65)✅ [5774022692642492953]
66)⬆️ [6028205772117118673]
67)🔓 [6037496202990194718]
68)🔒 [6037249452824072506]
69)🖼 [6030466823290360017]
70)🖼 [6035128606563241721]
71)🤖 [6030400221232501136]
72)⭐️ [6030425896546996257]
73)⭐️ [6030680867280522811]
74)📁 [6037475557082403885]
75)📁 [5904219717073114606]
76)📄 [6034969813032374911]
77)🗑 [6039522349517115015]
78)🎶 [6037364759811068375]
79)🎶 [6037460610596212193]
80)👁 [6037397706505195857]
81)👁 [6037243349675544634]
82)👁 [5884097155341226387]
83)👁 [5935757052042285202]
84)⬇️ [6037157012242960559]
85)⬇️ [5884218166044791498]
86)⬇️ [6039802767931871481]
87)☁ [6028115612163641653]
88)⬇️ [6032745346390560408]
89)⬆️ [5963103826075456248]
90)⬇️ [5963087934696459905]
91)⬆️ [6039391666547201160]
92)📤 [6039573425268201570]
93)⬆️ [5776288820467077551]
94)🛡 [6030537007350944596]
95)🛡 [6032636795387121097]
96)🛡 [6030445631921721471]
97)📂 [6039630677182254664]
98)📂 [6039800856671424701]
99)📂 [6037373985400819577]
100)📂 [6039348811363520645]
"""


def expected_pairs(source: str) -> tuple[tuple[str, str], ...]:
    pairs = []
    for number, line in enumerate(source.strip().splitlines(), start=1):
        left, identifier = line.split(" [")
        index, glyph = left.split(")")
        assert int(index) == number and identifier.endswith("]")
        pairs.append((glyph, identifier[:-1]))
    return tuple(pairs)


@pytest.mark.parametrize(
    ("name", "source", "count"),
    [("summon_bot", SUMMON_GOLDEN, 60), ("telegram_ios", IOS_GOLDEN, 100)],
)
def test_new_lists_match_every_numbered_user_pair(name, source, count):
    expected = expected_pairs(source)
    assert len(expected) == count
    assert PALETTES[name] == expected
    assert all(palette_icon(name, i) == expected[i - 1] for i in range(1, count + 1))


@pytest.mark.parametrize(("name", "count"), [("news", 100), ("rijif", 200), ("boseh", 200)])
def test_repeated_lists_match_every_numbered_pair_from_latest_message(name, count):
    fixture = Path(__file__).parent / "fixtures" / "repeated_emoji_lists.txt"
    text = fixture.read_text(encoding="utf-8")
    body = text.split(f"[{name}]\n", 1)[1].split("\n[", 1)[0]
    expected = expected_pairs(body)
    assert len(expected) == count
    assert PALETTES[name] == expected


@pytest.mark.parametrize(
    ("role", "pack", "number"),
    [
        ("SUMMON_BRAND", "summon_bot", 34),
        ("OPEN_COLLECTION", "summon_bot", 60),
        ("SERIES_TAG", "summon_bot", 1),
        ("RARITY_STAR", "summon_bot", 5),
        ("REWARD_COIN", "summon_bot", 48),
        ("GIFT_ACTION", "summon_bot", 19),
        ("DONE", "summon_bot", 37),
        ("ALERT", "summon_bot", 38),
        ("NOTE", "telegram_ios", 22),
        ("NOTE_EDIT", "telegram_ios", 30),
        ("NOTE_DELETE", "telegram_ios", 77),
        ("RULES", "telegram_ios", 76),
        ("PRIVATE", "telegram_ios", 68),
        ("ADMIN_SHIELD", "telegram_ios", 94),
    ],
)
def test_new_semantic_roles_use_exact_ids_and_unicode(role, pack, number):
    glyph, identifier = palette_icon(pack, number)
    emoji = Emoji[role]
    assert emoji.value == (glyph, identifier)
    assert emoji.render(premium=True) == f'<tg-emoji emoji-id="{identifier}">{glyph}</tg-emoji>'
    assert emoji.render(premium=False) == glyph


def test_field_icons_render_in_standard_and_rich_html_without_unescaped_data():
    card = Card(
        "Title <safe>",
        fields=(("Series <tag>", "A & B"), ("Reward", "+40"), ("Plain", "value")),
        field_icons=(Emoji.SERIES_TAG, Emoji.REWARD_COIN, None),
    )
    for rendered in (card.telegram_html(), card.rich_html()):
        assert Emoji.SERIES_TAG.value[1] in rendered
        assert Emoji.REWARD_COIN.value[1] in rendered
        assert "&lt;safe&gt;" in rendered and "&lt;tag&gt;" in rendered and "A &amp; B" in rendered
        assert "<safe>" not in rendered and "<tag>" not in rendered
    for rendered in (card.telegram_html(premium=False), card.rich_html(premium=False)):
        assert "tg-emoji" not in rendered
        assert "🏷" in rendered and "🪙" in rendered


def test_field_icon_count_must_match_fields():
    with pytest.raises(ValueError, match="Field icons"):
        Card(
            "Ready", fields=(("Points", "40"),), field_icons=(Emoji.REWARD_COIN, Emoji.RARITY_STAR)
        )


async def test_multiple_new_icons_fall_back_once_without_losing_button_actions():
    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramBadRequest(
            method=SendMessage(chat_id=123, text="x"), message="CUSTOM_EMOJI_INVALID"
        ),
        "sent",
    ]
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Open",
                    web_app=WebAppInfo(url="https://miniapp.example"),
                    icon_custom_emoji_id=Emoji.OPEN_COLLECTION.value[1],
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="Accept",
                    callback_data="gift:accept:abc",
                    icon_custom_emoji_id=Emoji.GIFT_ACTION.value[1],
                    style="success",
                ),
            ]
        ]
    )
    card = Card(
        "Ready",
        emoji=Emoji.SUMMON_BRAND,
        fields=(("Points", "40"),),
        field_icons=(Emoji.REWARD_COIN,),
    )
    assert await send_card(bot, 123, card, reply_markup=markup) == "sent"
    first, second = bot.send_message.await_args_list
    assert Emoji.SUMMON_BRAND.value[1] in first.kwargs["text"]
    assert Emoji.REWARD_COIN.value[1] in first.kwargs["text"]
    assert "tg-emoji" not in second.kwargs["text"]
    assert "✨" in second.kwargs["text"] and "🪙" in second.kwargs["text"]
    opened, accepted = second.kwargs["reply_markup"].inline_keyboard[0]
    assert opened.web_app.url == "https://miniapp.example" and opened.text == "💎 Open"
    assert accepted.callback_data == "gift:accept:abc" and accepted.text == "🎁 Accept"
    assert opened.icon_custom_emoji_id is accepted.icon_custom_emoji_id is None
    assert markup.inline_keyboard[0][0].icon_custom_emoji_id == Emoji.OPEN_COLLECTION.value[1]


@pytest.mark.parametrize("pack", ["summon_bot", "telegram_ios"])
def test_every_new_button_has_a_unicode_fallback_and_keeps_actions(pack):
    for glyph, identifier in PALETTES[pack]:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Open", callback_data="action:ok", icon_custom_emoji_id=identifier
                    )
                ]
            ]
        )
        restored = fallback_keyboard(markup).inline_keyboard[0][0]
        assert restored.text == f"{glyph} Open"
        assert restored.icon_custom_emoji_id is None
        assert restored.callback_data == "action:ok"


async def test_rich_rejection_strips_every_new_field_icon_and_preserves_actions():
    from aiogram.methods import SendRichMessage
    from aiogram.types import InputRichMessage

    bot = AsyncMock()
    bot.send_rich_message.side_effect = TelegramBadRequest(
        method=SendRichMessage(chat_id=123, rich_message=InputRichMessage(html="<p>x</p>")),
        message="unsupported rich message",
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Open",
                    web_app=WebAppInfo(url="https://miniapp.example"),
                    icon_custom_emoji_id=Emoji.OPEN_COLLECTION.value[1],
                )
            ]
        ]
    )
    card = Card(
        "Summary",
        emoji=Emoji.SUMMON_BRAND,
        fields=(("Series", "A & B"), ("Reward", "40")),
        field_icons=(Emoji.SERIES_TAG, Emoji.REWARD_COIN),
    )
    await send_card(bot, 123, card, reply_markup=markup, bot_api_rich=True)
    rich_html = bot.send_rich_message.await_args.kwargs["rich_message"].html
    assert Emoji.SERIES_TAG.value[1] in rich_html
    assert Emoji.REWARD_COIN.value[1] in rich_html
    plain = bot.send_message.await_args.kwargs
    assert "tg-emoji" not in plain["text"] and "A &amp; B" in plain["text"]
    assert "🏷" in plain["text"] and "🪙" in plain["text"]
    button = plain["reply_markup"].inline_keyboard[0][0]
    assert button.text == "💎 Open" and button.web_app.url == "https://miniapp.example"


async def test_disabled_premium_does_not_send_new_custom_emoji_ids():
    bot = AsyncMock()
    card = Card(
        "Summary",
        emoji=Emoji.SUMMON_BRAND,
        fields=(("Points", "40"),),
        field_icons=(Emoji.REWARD_COIN,),
    )
    await send_card(bot, 123, card, premium=False)
    text = bot.send_message.await_args.kwargs["text"]
    assert "tg-emoji" not in text
    assert Emoji.SUMMON_BRAND.value[0] in text and Emoji.REWARD_COIN.value[0] in text


async def test_ephemeral_field_icons_fall_back_without_exposing_private_data():
    from summon.bot.presentation import send_ephemeral_card

    bot = AsyncMock()
    method = SendMessage(chat_id=-100, text="x")
    bot.send_message.side_effect = [
        TelegramBadRequest(method=method, message="CUSTOM_EMOJI_INVALID"),
        "private-overlay",
    ]
    card = Card(
        "Private <profile>",
        emoji=Emoji.PRIVATE,
        fields=(("Points <label>", "A & B"),),
        field_icons=(Emoji.REWARD_COIN,),
    )
    assert await send_ephemeral_card(bot, -100, 42, card) == "private-overlay"
    assert bot.send_message.await_count == 2
    first, fallback = bot.send_message.await_args_list
    assert Emoji.REWARD_COIN.value[1] in first.kwargs["text"]
    for call in (first, fallback):
        assert call.kwargs["chat_id"] == -100
        assert call.kwargs["ephemeral_message_parameters"].receiver_user_id == 42
        assert "&lt;profile&gt;" in call.kwargs["text"]
        assert "&lt;label&gt;" in call.kwargs["text"] and "A &amp; B" in call.kwargs["text"]
    assert "tg-emoji" not in fallback.kwargs["text"]
    assert Emoji.REWARD_COIN.value[0] in fallback.kwargs["text"]


async def test_timeout_after_ephemeral_emoji_rejection_never_falls_back_publicly_or_to_dm():
    from summon.bot.presentation import send_ephemeral_card

    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramBadRequest(
            method=SendMessage(chat_id=-100, text="x"), message="CUSTOM_EMOJI_INVALID"
        ),
        TimeoutError("delivery unknown"),
    ]
    card = Card("Private", fields=(("Points", "40"),), field_icons=(Emoji.REWARD_COIN,))
    with pytest.raises(TimeoutError):
        await send_ephemeral_card(bot, -100, 42, card)
    assert bot.send_message.await_count == 2
    assert all(call.kwargs["chat_id"] == -100 for call in bot.send_message.await_args_list)
    assert all(
        call.kwargs["ephemeral_message_parameters"].receiver_user_id == 42
        for call in bot.send_message.await_args_list
    )


async def test_rejected_ephemeral_and_dm_icons_keep_field_contents_private():
    from summon.bot.presentation import send_ephemeral_card

    bot = AsyncMock()
    group = SendMessage(chat_id=-100, text="x")
    dm = SendMessage(chat_id=42, text="x")
    bot.send_message.side_effect = [
        TelegramBadRequest(method=group, message="CUSTOM_EMOJI_INVALID"),
        TelegramBadRequest(method=group, message="EPHEMERAL_UNSUPPORTED"),
        TelegramBadRequest(method=dm, message="CUSTOM_EMOJI_INVALID"),
        "private-dm",
    ]
    card = Card("Profile", fields=(("Points", "40"),), field_icons=(Emoji.REWARD_COIN,))
    assert await send_ephemeral_card(bot, -100, 42, card) == "private-dm"
    attempts = bot.send_message.await_args_list
    assert [call.kwargs["chat_id"] for call in attempts] == [-100, -100, 42, 42]
    assert all(
        call.kwargs["ephemeral_message_parameters"].receiver_user_id == 42 for call in attempts[:2]
    )
    assert all("ephemeral_message_parameters" not in call.kwargs for call in attempts[2:])
    assert "tg-emoji" not in attempts[-1].kwargs["text"]
    assert Emoji.REWARD_COIN.value[0] in attempts[-1].kwargs["text"]
