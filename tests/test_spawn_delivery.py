"""A summon without its artwork must never masquerade as a fair challenge."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendPhoto
from aiogram.types import Update

from summon.bot.handlers import build_router
from summon.bot.presentation import Emoji
from summon.config import Settings
from summon.models import GroupSettings


def setup_spawn(image_url, *, premium=False):
    character = {
        "_id": "aiko",
        "name": "Aiko",
        "category": "Sky",
        "rarity": "rare",
        "image_url": image_url,
    }
    store = SimpleNamespace(
        groups=SimpleNamespace(
            db=object(),
            get=AsyncMock(return_value={"settings": GroupSettings().model_dump()}),
            ensure=AsyncMock(),
        ),
        collection=SimpleNamespace(
            record_message=AsyncMock(
                return_value={
                    "settings": {"spawn_enabled": True, "spawn_every": 20},
                    "message_count": 20,
                }
            ),
            reserve_spawn=AsyncMock(return_value={"id": "spawn-one"}),
            mark_delivery_failed=AsyncMock(),
        ),
        catalog=SimpleNamespace(choose=AsyncMock(return_value=character)),
        trades=object(),
        gifts=object(),
    )
    dp = Dispatcher()
    dp.include_router(
        build_router(
            store,
            Settings(
                _env_file=None,
                webapp_url="",
                bot_api_rich_enabled=False,
                premium_emoji_enabled=premium,
            ),
        )
    )
    return dp, store


def ordinary_message(bot):
    return Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 9,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": -100, "type": "supergroup", "title": "Test"},
                "from": {"id": 42, "is_bot": False, "first_name": "Member"},
                "text": "Hello everyone",
            },
        },
        context={"bot": bot},
    )


async def test_definitely_rejected_artwork_cancels_instead_of_posting_an_unguessable_spawn():
    dp, store = setup_spawn("https://example.com/aiko.webp")
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    error = TelegramBadRequest(
        method=SendPhoto(chat_id=-100, photo="https://example.com/aiko.webp"), message="bad image"
    )
    with (
        patch.object(Bot, "send_photo", new_callable=AsyncMock, side_effect=error) as photo,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as card,
    ):
        await dp.feed_update(bot, ordinary_message(bot))
    photo.assert_awaited_once()
    store.collection.mark_delivery_failed.assert_awaited_once_with(-100, "spawn-one")
    # If a notice is sent, it must not advertise a guessing challenge.
    if card.await_count:
        assert "Guess their name" not in card.await_args.args[2].body
    await bot.session.close()


async def test_local_artwork_without_a_public_url_cancels_spawn():
    dp, store = setup_spawn("/art/aiko.webp")
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with (
        patch.object(Bot, "send_photo", new_callable=AsyncMock) as photo,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as card,
    ):
        await dp.feed_update(bot, ordinary_message(bot))
    photo.assert_not_awaited()
    store.collection.mark_delivery_failed.assert_awaited_once_with(-100, "spawn-one")
    if card.await_count:
        assert "Guess their name" not in card.await_args.args[2].body
    await bot.session.close()


async def test_ambiguous_photo_timeout_does_not_cancel_or_duplicate_delivery():
    dp, store = setup_spawn("https://example.com/aiko.webp")
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    error = TelegramNetworkError(
        method=SendPhoto(chat_id=-100, photo="https://example.com/aiko.webp"), message="timeout"
    )
    with (
        patch.object(Bot, "send_photo", new_callable=AsyncMock, side_effect=error) as photo,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as card,
        pytest.raises(TelegramNetworkError),
    ):
        await dp.feed_update(bot, ordinary_message(bot))
    photo.assert_awaited_once()
    card.assert_not_awaited()
    store.collection.mark_delivery_failed.assert_not_awaited()
    await bot.session.close()


async def test_successful_artwork_keeps_spawn_active_without_a_second_message():
    dp, store = setup_spawn("https://example.com/aiko.webp")
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with (
        patch.object(Bot, "send_photo", new_callable=AsyncMock) as photo,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as card,
    ):
        await dp.feed_update(bot, ordinary_message(bot))
    photo.assert_awaited_once()
    assert photo.await_args.kwargs["photo"] == "https://example.com/aiko.webp"
    store.collection.mark_delivery_failed.assert_not_awaited()
    card.assert_not_awaited()
    await bot.session.close()


async def test_forbidden_photo_delivery_cancels_the_reservation():
    from aiogram.exceptions import TelegramForbiddenError

    dp, store = setup_spawn("https://example.com/aiko.webp")
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    error = TelegramForbiddenError(
        method=SendPhoto(chat_id=-100, photo="https://example.com/aiko.webp"), message="forbidden"
    )
    with (
        patch.object(Bot, "send_photo", new_callable=AsyncMock, side_effect=error) as photo,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as card,
    ):
        await dp.feed_update(bot, ordinary_message(bot))
    photo.assert_awaited_once()
    card.assert_not_awaited()
    store.collection.mark_delivery_failed.assert_awaited_once_with(-100, "spawn-one")
    await bot.session.close()


async def test_rejected_premium_caption_retries_photo_with_unicode_without_cancelling():
    dp, store = setup_spawn("https://example.com/aiko.webp", premium=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    error = TelegramBadRequest(
        method=SendPhoto(chat_id=-100, photo="https://example.com/aiko.webp"),
        message="CUSTOM_EMOJI_INVALID",
    )
    with (
        patch.object(Bot, "send_photo", new_callable=AsyncMock, side_effect=[error, True]) as photo,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as card,
    ):
        await dp.feed_update(bot, ordinary_message(bot))
    assert photo.await_count == 2
    first, second = photo.await_args_list
    assert first.kwargs["photo"] == second.kwargs["photo"] == "https://example.com/aiko.webp"
    assert Emoji.SPARK.value[1] in first.kwargs["caption"]
    assert "tg-emoji" in first.kwargs["caption"]
    assert "tg-emoji" not in second.kwargs["caption"]
    assert Emoji.SPARK.value[0] in second.kwargs["caption"]
    card.assert_not_awaited()
    store.collection.mark_delivery_failed.assert_not_awaited()
    await bot.session.close()


async def test_timeout_on_premium_photo_fallback_is_not_retried_or_cancelled():
    dp, store = setup_spawn("https://example.com/aiko.webp", premium=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    method = SendPhoto(chat_id=-100, photo="https://example.com/aiko.webp")
    emoji_error = TelegramBadRequest(method=method, message="CUSTOM_EMOJI_INVALID")
    timeout = TelegramNetworkError(method=method, message="timeout")
    with (
        patch.object(
            Bot, "send_photo", new_callable=AsyncMock, side_effect=[emoji_error, timeout]
        ) as photo,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as card,
        pytest.raises(TelegramNetworkError),
    ):
        await dp.feed_update(bot, ordinary_message(bot))
    assert photo.await_count == 2
    card.assert_not_awaited()
    store.collection.mark_delivery_failed.assert_not_awaited()
    await bot.session.close()


async def test_both_caption_attempts_definitely_rejected_cancel_once():
    dp, store = setup_spawn("https://example.com/aiko.webp", premium=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    method = SendPhoto(chat_id=-100, photo="https://example.com/aiko.webp")
    errors = [
        TelegramBadRequest(method=method, message="CUSTOM_EMOJI_INVALID"),
        TelegramBadRequest(method=method, message="bad image"),
    ]
    with (
        patch.object(Bot, "send_photo", new_callable=AsyncMock, side_effect=errors) as photo,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as card,
    ):
        await dp.feed_update(bot, ordinary_message(bot))
    assert photo.await_count == 2
    card.assert_not_awaited()
    store.collection.mark_delivery_failed.assert_awaited_once_with(-100, "spawn-one")
    await bot.session.close()
