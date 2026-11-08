"""A summon without its artwork must never masquerade as a fair challenge."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from wzgram import Client, raw
from wzgram.errors import BadRequest, Forbidden
from wzgram.handlers import RawUpdateHandler
from wzgram.types import User

from summon.bot.handlers import register_handlers
from summon.bot.presentation import Emoji
from summon.config import Settings
from summon.models import GroupSettings


async def setup_spawn(image_url, *, premium=False):
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
    app = Client(
        "offline-test",
        api_id=123,
        api_hash="test",
        bot_token="123:fake",
        in_memory=True,
        workers=1,
        skip_updates=True,
    )
    app.me = User(id=123, is_bot=True, username="summon_test", first_name="Bot")
    register_handlers(
        app,
        store,
        Settings(_env_file=None, webapp_url="", rich_enabled=False, premium_emoji_enabled=premium),
    )
    await asyncio.sleep(0)
    return app, store


async def dispatch_message(app):
    """Use the native raw-update dispatcher and wait until all handler groups finish."""
    finished = asyncio.Event()

    async def after_update(_client, _update, _users, _chats):
        finished.set()

    app.add_handler(RawUpdateHandler(after_update), group=100)
    await asyncio.sleep(0)
    actor = raw.types.User(
        id=42, first_name="Member", access_hash=42, usernames=[], restriction_reason=[]
    )
    group = raw.types.Chat(
        id=100,
        title="Test",
        photo=raw.types.ChatPhotoEmpty(),
        participants_count=2,
        date=int(datetime.now(UTC).timestamp()),
        version=1,
    )
    message = raw.types.Message(
        id=9,
        peer_id=raw.types.PeerChat(chat_id=100),
        from_id=raw.types.PeerUser(user_id=42),
        date=int(datetime.now(UTC).timestamp()),
        message="Hello everyone",
        entities=[],
        restriction_reason=[],
    )
    update = raw.types.UpdateNewMessage(message=message, pts=1, pts_count=1)
    await app.dispatcher.start()
    try:
        await app.dispatcher.updates_queue.put((update, {42: actor}, {100: group}))
        await asyncio.wait_for(finished.wait(), 2)
    finally:
        await app.dispatcher.stop()


async def test_definitely_rejected_artwork_cancels_instead_of_posting_an_unguessable_spawn():
    app, store = await setup_spawn("https://example.com/aiko.webp")
    with (
        patch.object(
            app, "send_photo", new_callable=AsyncMock, side_effect=BadRequest("BAD_IMAGE")
        ) as photo,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as card,
    ):
        await dispatch_message(app)
    photo.assert_awaited_once()
    store.collection.mark_delivery_failed.assert_awaited_once_with(-100, "spawn-one")
    if card.await_count:
        assert "Guess their name" not in card.await_args.args[2].body


async def test_local_artwork_without_a_public_url_cancels_spawn():
    app, store = await setup_spawn("/art/aiko.webp")
    with (
        patch.object(app, "send_photo", new_callable=AsyncMock) as photo,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as card,
    ):
        await dispatch_message(app)
    photo.assert_not_awaited()
    store.collection.mark_delivery_failed.assert_awaited_once_with(-100, "spawn-one")
    if card.await_count:
        assert "Guess their name" not in card.await_args.args[2].body


async def test_ambiguous_photo_timeout_does_not_cancel_or_duplicate_delivery():
    app, store = await setup_spawn("https://example.com/aiko.webp")
    with (
        patch.object(
            app, "send_photo", new_callable=AsyncMock, side_effect=TimeoutError("timeout")
        ) as photo,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as card,
    ):
        await dispatch_message(app)
    photo.assert_awaited_once()
    card.assert_not_awaited()
    store.collection.mark_delivery_failed.assert_not_awaited()


async def test_successful_artwork_keeps_spawn_active_without_a_second_message():
    app, store = await setup_spawn("https://example.com/aiko.webp")
    with (
        patch.object(app, "send_photo", new_callable=AsyncMock) as photo,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as card,
    ):
        await dispatch_message(app)
    photo.assert_awaited_once()
    assert photo.await_args.kwargs["photo"] == "https://example.com/aiko.webp"
    store.collection.mark_delivery_failed.assert_not_awaited()
    card.assert_not_awaited()


async def test_forbidden_photo_delivery_cancels_the_reservation():
    app, store = await setup_spawn("https://example.com/aiko.webp")
    with (
        patch.object(
            app, "send_photo", new_callable=AsyncMock, side_effect=Forbidden("FORBIDDEN")
        ) as photo,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as card,
    ):
        await dispatch_message(app)
    photo.assert_awaited_once()
    card.assert_not_awaited()
    store.collection.mark_delivery_failed.assert_awaited_once_with(-100, "spawn-one")


async def test_rejected_premium_caption_retries_photo_with_unicode_without_cancelling():
    app, store = await setup_spawn("https://example.com/aiko.webp", premium=True)
    with (
        patch.object(
            app,
            "send_photo",
            new_callable=AsyncMock,
            side_effect=[BadRequest("CUSTOM_EMOJI_INVALID"), True],
        ) as photo,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as card,
    ):
        await dispatch_message(app)
    assert photo.await_count == 2
    first, second = photo.await_args_list
    assert first.kwargs["photo"] == second.kwargs["photo"] == "https://example.com/aiko.webp"
    assert Emoji.SPARK.value[1] in first.kwargs["caption"]
    assert Emoji.SERIES_TAG.value[1] in first.kwargs["caption"]
    assert Emoji.RARITY_STAR.value[1] in first.kwargs["caption"]
    assert "tg-emoji" in first.kwargs["caption"]
    assert "tg-emoji" not in second.kwargs["caption"]
    assert Emoji.SPARK.value[0] in second.kwargs["caption"]
    assert Emoji.SERIES_TAG.value[0] in second.kwargs["caption"]
    assert Emoji.RARITY_STAR.value[0] in second.kwargs["caption"]
    card.assert_not_awaited()
    store.collection.mark_delivery_failed.assert_not_awaited()


async def test_timeout_on_premium_photo_fallback_is_not_retried_or_cancelled():
    app, store = await setup_spawn("https://example.com/aiko.webp", premium=True)
    errors = [BadRequest("CUSTOM_EMOJI_INVALID"), TimeoutError("timeout")]
    with (
        patch.object(app, "send_photo", new_callable=AsyncMock, side_effect=errors) as photo,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as card,
    ):
        await dispatch_message(app)
    assert photo.await_count == 2
    card.assert_not_awaited()
    store.collection.mark_delivery_failed.assert_not_awaited()


async def test_both_caption_attempts_definitely_rejected_cancel_once():
    app, store = await setup_spawn("https://example.com/aiko.webp", premium=True)
    errors = [BadRequest("CUSTOM_EMOJI_INVALID"), BadRequest("BAD_IMAGE")]
    with (
        patch.object(app, "send_photo", new_callable=AsyncMock, side_effect=errors) as photo,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as card,
    ):
        await dispatch_message(app)
    assert photo.await_count == 2
    card.assert_not_awaited()
    store.collection.mark_delivery_failed.assert_awaited_once_with(-100, "spawn-one")


async def test_spawn_photo_contains_supplied_series_and_rarity_icons():
    app, _ = await setup_spawn("https://example.com/aiko.webp", premium=True)
    with patch.object(app, "send_photo", new_callable=AsyncMock) as photo:
        await dispatch_message(app)
    caption = photo.await_args.kwargs["caption"]
    assert Emoji.SERIES_TAG.value[1] in caption
    assert Emoji.RARITY_STAR.value[1] in caption
    assert "Sky" in caption and "Rare" in caption
