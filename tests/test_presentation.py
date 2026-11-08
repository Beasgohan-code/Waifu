from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from wzgram.errors import BadRequest

from summon.bot.presentation import Card, Emoji, send_card


def test_vobject_escapes_data_and_premium_has_unicode_fallback():
    card = Card(
        "Collection <s>", "Hello & welcome", emoji=Emoji.SPARK, fields=(("Name", "<Mika>"),)
    )
    rich = card.rich_html(premium=True)
    html = card.telegram_html(premium=True)
    assert "<Mika>" not in html
    assert "&lt;Mika&gt;" in html
    assert '<tg-emoji emoji-id="' in html
    assert '<tg-emoji emoji-id="' in rich
    assert "✨" in card.telegram_html(premium=False)
    assert "tg-emoji" not in card.telegram_html(premium=False)


async def test_rich_rejection_falls_back_to_standard_message():
    bot = AsyncMock()
    client = AsyncMock()
    client.send_rich_message.side_effect = BadRequest("BAD_REQUEST")
    await send_card(bot, 123, Card("SUMMON", "Ready"), rich_client=client)
    assert client.send_rich_message.await_count == 1
    assert bot.send_message.await_count == 1


async def test_ambiguous_rich_timeout_is_never_retried():
    bot = AsyncMock()
    client = AsyncMock()
    client.send_rich_message.side_effect = TimeoutError("uncertain delivery")
    with pytest.raises(TimeoutError):
        await send_card(bot, 123, Card("SUMMON", "Ready"), rich_client=client)
    bot.send_message.assert_not_awaited()


async def test_rejected_premium_entity_retries_once_without_it():
    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramBadRequest(
            method=SendMessage(chat_id=123, text="x"), message="CUSTOM_EMOJI_INVALID"
        ),
        "sent",
    ]
    result = await send_card(bot, 123, Card("SUMMON", "Ready", emoji=Emoji.SPARK))
    assert result == "sent"
    assert bot.send_message.await_count == 2
    assert "tg-emoji" in bot.send_message.await_args_list[0].kwargs["text"]
    assert "tg-emoji" not in bot.send_message.await_args_list[1].kwargs["text"]


async def test_unrelated_bad_request_does_not_retry():
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramBadRequest(
        method=SendMessage(chat_id=123, text="x"), message="chat not found"
    )
    with pytest.raises(TelegramBadRequest):
        await send_card(bot, 123, Card("SUMMON", "Ready", emoji=Emoji.SPARK))
    assert bot.send_message.await_count == 1


async def test_rejected_button_icon_preserves_callback_and_unicode():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Accept",
                    callback_data="trade:accept:abc",
                    style="success",
                    icon_custom_emoji_id=Emoji.CHECK.value[1],
                )
            ]
        ]
    )
    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramBadRequest(
            method=SendMessage(chat_id=123, text="x"), message="CUSTOM_EMOJI_INVALID"
        ),
        "sent",
    ]
    await send_card(bot, 123, Card("Trade", "Ready"), reply_markup=markup)
    fallback = bot.send_message.await_args_list[1].kwargs["reply_markup"].inline_keyboard[0][0]
    assert fallback.callback_data == "trade:accept:abc"
    assert fallback.text.startswith("✅")
    assert fallback.icon_custom_emoji_id is None
    assert markup.inline_keyboard[0][0].icon_custom_emoji_id is not None


async def test_ephemeral_rejection_falls_back_to_dm_not_public_group():
    from summon.bot.presentation import send_ephemeral_card

    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramBadRequest(
            method=SendMessage(chat_id=-100, text="x"), message="EPHEMERAL_UNSUPPORTED"
        ),
        "private",
    ]
    result = await send_ephemeral_card(
        bot, -100, 42, Card("Inventory", "Private details"), premium=False
    )
    assert result == "private"
    first, second = bot.send_message.await_args_list
    assert first.kwargs["chat_id"] == -100
    assert first.kwargs["ephemeral_message_parameters"].receiver_user_id == 42
    assert second.kwargs["chat_id"] == 42
    assert "ephemeral_message_parameters" not in second.kwargs


async def test_ephemeral_timeout_never_causes_duplicate_or_public_retry():
    from summon.bot.presentation import send_ephemeral_card

    bot = AsyncMock()
    bot.send_message.side_effect = TimeoutError("delivery unknown")
    with pytest.raises(TimeoutError):
        await send_ephemeral_card(bot, -100, 42, Card("Inventory", "Private details"))
    assert bot.send_message.await_count == 1


async def test_rich_sender_uses_wzgram_signature_without_retrying_success():
    from wzgram.types import InputRichMessage

    class StrictWzgramClient:
        def __init__(self):
            self.received = None

        async def send_rich_message(self, chat_id, rich_text, *, message_thread_id=None):
            self.received = (chat_id, rich_text, message_thread_id)
            return "sent"

    client = StrictWzgramClient()
    bot = AsyncMock()
    result = await send_card(
        bot, 123, Card("SUMMON", "Ready"), rich_client=client, premium=False, message_thread_id=5
    )
    assert result == "sent"
    assert client.received[0] == 123
    assert isinstance(client.received[1], InputRichMessage)
    assert client.received[2] == 5
    bot.send_message.assert_not_awaited()


def test_installed_wzgram_rich_signature_is_supported():
    import inspect

    from wzgram import Client

    assert "rich_text" in inspect.signature(Client.send_rich_message).parameters


def test_richgram_builder_output_serializes_with_wzgram():
    from wzgram.types import InputRichMessage

    card = Card("Hello", "Ready", fields=(("Name", "<Mika>"),), details=("More", "Details"))
    rich_input = InputRichMessage(html=card.rich_html(premium=False))
    raw = rich_input.write()
    assert type(raw).__name__ == "InputRichMessageHTML"
    assert "<table" in raw.html
    assert "&lt;Mika&gt;" in raw.html
    assert "<details" in raw.html


async def test_ephemeral_reply_uses_the_incoming_ephemeral_message_as_trigger():
    from summon.bot.presentation import send_ephemeral_card

    bot = AsyncMock()
    await send_ephemeral_card(
        bot,
        -100,
        42,
        Card("Inventory"),
        premium=False,
        incoming_ephemeral_id=999,
        message_thread_id=5,
    )
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == -100
    assert kwargs["ephemeral_message_parameters"].receiver_user_id == 42
    assert kwargs["reply_parameters"].ephemeral_message_id == 999
    assert kwargs["message_thread_id"] == 5


def test_rich_table_uses_bot_api_10_3_attributes():
    card = Card("Stats", fields=(("Owned", "5"),))
    assert "<table bordered compact>" in card.rich_html(premium=False)
    assert 'border="1"' not in card.rich_html(premium=False)


async def test_bot_api_rich_card_preserves_keyboard_callbacks():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage

    bot = AsyncMock()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Accept", callback_data="gift:accept:abc")]]
    )
    await send_card(bot, -100, Card("Gift"), reply_markup=markup, bot_api_rich=True)
    kwargs = bot.send_rich_message.await_args.kwargs
    assert isinstance(kwargs["rich_message"], InputRichMessage)
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "gift:accept:abc"
    bot.send_message.assert_not_awaited()


async def test_bot_api_rich_rejection_falls_back_to_plain_keyboard_once():
    from aiogram.methods import SendRichMessage
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage

    bot = AsyncMock()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Accept",
                    callback_data="gift:accept:abc",
                    style="success",
                    icon_custom_emoji_id=Emoji.CHECK.value[1],
                )
            ]
        ]
    )
    bot.send_rich_message.side_effect = TelegramBadRequest(
        method=SendRichMessage(chat_id=-100, rich_message=InputRichMessage(html="<p>x</p>")),
        message="unsupported rich message",
    )
    await send_card(bot, -100, Card("Gift"), reply_markup=markup, bot_api_rich=True)
    button = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "gift:accept:abc"
    assert button.icon_custom_emoji_id is None
    assert "tg-emoji" not in bot.send_message.await_args.kwargs["text"]


async def test_bot_api_rich_timeout_does_not_retry():
    bot = AsyncMock()
    bot.send_rich_message.side_effect = TimeoutError("delivery unknown")
    with pytest.raises(TimeoutError):
        await send_card(bot, 1, Card("Hello"), bot_api_rich=True)
    bot.send_message.assert_not_awaited()


async def test_generic_invalid_button_type_is_not_treated_as_emoji_rejection():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    bot = AsyncMock()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Accept",
                    callback_data="gift:accept:abc",
                    icon_custom_emoji_id=Emoji.GIFT.value[1],
                )
            ]
        ]
    )
    bot.send_message.side_effect = TelegramBadRequest(
        method=SendMessage(chat_id=123, text="x"), message="BUTTON_TYPE_INVALID"
    )
    with pytest.raises(TelegramBadRequest):
        await send_card(bot, 123, Card("Ready", emoji=Emoji.GIFT), reply_markup=markup)
    bot.send_message.assert_awaited_once()


async def test_standard_message_timeout_never_retries_premium_emoji():
    bot = AsyncMock()
    bot.send_message.side_effect = TimeoutError("delivery unknown")
    with pytest.raises(TimeoutError):
        await send_card(bot, 123, Card("Ready", emoji=Emoji.SHIELD))
    bot.send_message.assert_awaited_once()


async def test_timeout_after_rejected_emoji_does_not_cause_a_third_send():
    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramBadRequest(
            method=SendMessage(chat_id=123, text="x"), message="CUSTOM_EMOJI_INVALID"
        ),
        TimeoutError("fallback delivery unknown"),
    ]
    with pytest.raises(TimeoutError):
        await send_card(bot, 123, Card("Ready", emoji=Emoji.GIFT))
    assert bot.send_message.await_count == 2


async def test_disabled_premium_rich_fallback_does_not_duplicate_unicode_prefix():
    from aiogram.methods import SendRichMessage
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage

    from summon.bot.presentation import button_label

    bot = AsyncMock()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=button_label("Accept", Emoji.GIFT, premium=False),
                    callback_data="gift:accept:abc",
                    style="success",
                )
            ]
        ]
    )
    bot.send_rich_message.side_effect = TelegramBadRequest(
        method=SendRichMessage(chat_id=123, rich_message=InputRichMessage(html="<p>x</p>")),
        message="unsupported rich message",
    )
    await send_card(
        bot,
        123,
        Card("Gift", emoji=Emoji.GIFT),
        bot_api_rich=True,
        premium=False,
        reply_markup=markup,
    )
    button = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == f"{Emoji.GIFT.value[0]} Accept"
    assert button.callback_data == "gift:accept:abc"
    assert button.icon_custom_emoji_id is None and button.style is None
    assert "tg-emoji" not in bot.send_message.await_args.kwargs["text"]


async def test_rejected_new_pack_icons_fall_back_without_losing_webapp_or_callback():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

    bot = AsyncMock()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Accept gift",
                    callback_data="gift:accept:abc",
                    icon_custom_emoji_id=Emoji.GIFT.value[1],
                    style="success",
                ),
                InlineKeyboardButton(
                    text="Open",
                    web_app=WebAppInfo(url="https://miniapp.example"),
                    icon_custom_emoji_id=Emoji.CROWN.value[1],
                    style="primary",
                ),
            ]
        ]
    )
    bot.send_message.side_effect = [
        TelegramBadRequest(
            method=SendMessage(chat_id=123, text="x"), message="CUSTOM_EMOJI_INVALID"
        ),
        "sent",
    ]
    assert (
        await send_card(bot, 123, Card("Ready", emoji=Emoji.SHIELD), reply_markup=markup) == "sent"
    )
    first, second = bot.send_message.await_args_list
    assert Emoji.SHIELD.value[1] in first.kwargs["text"]
    assert "tg-emoji" not in second.kwargs["text"]
    accept, opened = second.kwargs["reply_markup"].inline_keyboard[0]
    assert accept.callback_data == "gift:accept:abc"
    assert accept.text == f"{Emoji.GIFT.value[0]} Accept gift"
    assert opened.web_app.url == "https://miniapp.example"
    assert opened.text == f"{Emoji.CROWN.value[0]} Open"
    assert accept.icon_custom_emoji_id is opened.icon_custom_emoji_id is None
    assert markup.inline_keyboard[0][0].icon_custom_emoji_id == Emoji.GIFT.value[1]
