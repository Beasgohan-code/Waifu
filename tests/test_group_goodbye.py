"""Optional leave notices must be opt-in, bounded, and never count as activity."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from wzgram import Client, raw
from wzgram.handlers import RawUpdateHandler
from wzgram.types import User

from summon.bot.handlers import register_handlers
from summon.bot.presentation import Emoji
from summon.config import Settings
from summon.models import GroupSettings, GroupSettingsUpdate


def test_goodbye_settings_are_backward_compatible_and_bounded():
    previous = GroupSettings.model_validate({})
    assert previous.goodbye_enabled is False
    assert previous.goodbye_text
    assert GroupSettingsUpdate(goodbye_enabled=True, goodbye_text="Take care!").model_dump(
        exclude_unset=True
    ) == {"goodbye_enabled": True, "goodbye_text": "Take care!"}
    with pytest.raises(ValidationError):
        GroupSettingsUpdate(goodbye_text="x" * 1001)


async def setup(enabled=True, *, text="Take care!", limited=(True, True)):
    groups = SimpleNamespace(
        db=object(),
        get=AsyncMock(
            return_value={"settings": {"goodbye_enabled": enabled, "goodbye_text": text}}
        ),
        ensure=AsyncMock(),
        record_spam=AsyncMock(),
    )
    store = SimpleNamespace(
        groups=groups,
        limiter=SimpleNamespace(allow=AsyncMock(side_effect=limited)),
        collection=SimpleNamespace(record_message=AsyncMock()),
        catalog=object(),
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
        Settings(_env_file=None, bot_api_rich_enabled=False, premium_emoji_enabled=False),
    )
    app._test_complete = asyncio.Queue()

    async def after_update(_client, _update, _users, _chats):
        app._test_complete.put_nowait(None)

    app.add_handler(RawUpdateHandler(after_update), group=100)
    await asyncio.sleep(0)
    await app.dispatcher.start()
    return app, store


def leave(_bot, *, member_id=42, name="Mika <friend>", is_bot=False, chat_type="supergroup"):
    now = int(datetime.now(UTC).timestamp())
    actor = raw.types.User(
        id=42, first_name="Mika", access_hash=42, usernames=[], restriction_reason=[]
    )
    departed = raw.types.User(
        id=member_id,
        first_name=name,
        bot=is_bot,
        access_hash=member_id,
        usernames=[],
        restriction_reason=[],
    )
    group = raw.types.Chat(
        id=100,
        title="Test",
        photo=raw.types.ChatPhotoEmpty(),
        participants_count=2,
        date=now,
        version=1,
    )
    peer = (
        raw.types.PeerUser(user_id=42)
        if chat_type == "private"
        else raw.types.PeerChat(chat_id=100)
    )
    message = raw.types.MessageService(
        id=22,
        peer_id=peer,
        from_id=raw.types.PeerUser(user_id=42),
        date=now,
        action=raw.types.MessageActionChatDeleteUser(user_id=member_id),
    )
    return (
        raw.types.UpdateNewMessage(message=message, pts=1, pts_count=1),
        {42: actor, member_id: departed},
        {100: group},
    )


async def dispatch(app, update):
    await app.dispatcher.updates_queue.put(update)
    await asyncio.wait_for(app._test_complete.get(), 2)


async def test_enabled_goodbye_sends_one_escaped_card_with_rate_limits():
    dp, store = await setup()
    bot = dp
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(dp, leave(bot))
    send.assert_awaited_once()
    card = send.await_args.args[2]
    assert "Mika <friend>" in card.title and card.body == "Take care!"
    assert card.emoji is Emoji.MOON
    assert "&lt;friend&gt;" in card.telegram_html(premium=False)
    assert store.limiter.allow.await_count == 2
    assert store.limiter.allow.await_args_list[0].args == ("group:-100:goodbye:42",)
    assert store.limiter.allow.await_args_list[1].args == ("group:-100:goodbye",)
    assert store.limiter.allow.await_args_list[0].kwargs == {"limit": 1, "seconds": 60}
    assert store.limiter.allow.await_args_list[1].kwargs == {"limit": 10, "seconds": 60}
    store.collection.record_message.assert_not_awaited()
    store.groups.record_spam.assert_not_awaited()
    await bot.dispatcher.stop()


async def test_disabled_goodbye_is_silent_and_does_not_spend_rate_limit():
    dp, store = await setup(enabled=False)
    bot = dp
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(dp, leave(bot))
    send.assert_not_awaited()
    store.limiter.allow.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()
    await bot.dispatcher.stop()


@pytest.mark.parametrize("limited", [(False,), (True, False)])
async def test_goodbye_respects_per_member_and_group_rate_limits(limited):
    dp, store = await setup(limited=limited)
    bot = dp
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(dp, leave(bot))
    send.assert_not_awaited()
    assert store.limiter.allow.await_count == len(limited)
    store.collection.record_message.assert_not_awaited()
    await bot.dispatcher.stop()


async def test_bot_departures_do_not_trigger_goodbye_or_lookup():
    dp, store = await setup()
    bot = dp
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(dp, leave(bot, member_id=999, name="Bot", is_bot=True))
    send.assert_not_awaited()
    store.groups.get.assert_not_awaited()
    store.limiter.allow.assert_not_awaited()
    await bot.dispatcher.stop()


async def test_goodbye_never_retries_an_ambiguous_send():
    dp, store = await setup()
    bot = dp
    error = ConnectionError("timeout")
    with (
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock, side_effect=error) as send,
    ):
        await dispatch(dp, leave(bot))
    send.assert_awaited_once()
    assert store.limiter.allow.await_count == 2
    store.collection.record_message.assert_not_awaited()
    await bot.dispatcher.stop()


async def test_goodbye_settings_update_preserves_other_legacy_group_fields(monkeypatch):
    from summon.repositories.groups import GroupRepository

    async def transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.groups.transaction", transaction)
    settings = GroupSettings().model_dump()
    settings.pop("goodbye_enabled")
    settings.pop("goodbye_text")
    settings["link_filter"] = True
    group = {"_id": -100, "title": "Test", "settings": settings, "total_messages": 20}
    db = SimpleNamespace(
        groups=SimpleNamespace(find_one=AsyncMock(return_value=group), update_one=AsyncMock()),
        activity=SimpleNamespace(insert_one=AsyncMock()),
    )
    repo = GroupRepository(object(), db)
    patch_values = {"goodbye_enabled": True, "goodbye_text": "Take care!"}
    result = await repo.update_settings(-100, 123, patch_values, datetime.now(UTC))
    assert result["settings"]["link_filter"] is True
    assert result["settings"]["goodbye_enabled"] is True
    assert db.groups.update_one.await_args.args[1] == {
        "$set": {"settings.goodbye_enabled": True, "settings.goodbye_text": "Take care!"}
    }
    assert db.groups.update_one.await_args.kwargs["session"] == "session"
    event = db.activity.insert_one.await_args
    assert event.kwargs["session"] == "session"
    assert event.args[0]["user_id"] == 123
    assert event.args[0]["fields"] == ["goodbye_enabled", "goodbye_text"]


async def test_goodbye_fixed_window_limits_work_with_shared_counter_store(monkeypatch):
    from summon.repositories.limits import RateLimiter

    class Counters:
        def __init__(self):
            self.rows = {}

        async def find_one_and_update(self, query, update, **_kwargs):
            row = self.rows.setdefault(query["_id"], {"count": 0, **update["$setOnInsert"]})
            row["count"] += 1
            return row

    now = datetime(2026, 11, 4, tzinfo=UTC)
    monkeypatch.setattr("summon.repositories.limits.utcnow", lambda: now)
    counters = Counters()
    dp, store = await setup()
    store.limiter = RateLimiter(SimpleNamespace(rate_limits=counters))
    bot = dp
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(dp, leave(bot, member_id=42))
        await dispatch(dp, leave(bot, member_id=42))  # per-member cap
        for user_id in range(43, 54):  # group cap stops after ten distinct notices
            await dispatch(dp, leave(bot, member_id=user_id))
    assert send.await_count == 10
    assert all(row["expires_at"] > now for row in counters.rows.values())
    assert all(key.startswith("api:") for key in counters.rows)
    store.collection.record_message.assert_not_awaited()
    await bot.dispatcher.stop()


async def test_unconfigured_group_does_not_send_goodbye():
    dp, store = await setup()
    store.groups.get.return_value = None
    bot = dp
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(dp, leave(bot))
    send.assert_not_awaited()
    store.limiter.allow.assert_not_awaited()
    await bot.dispatcher.stop()
