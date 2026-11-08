"""Exact-match auto replies must not bypass protection, privacy or spawn activity."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from wzgram import Client, raw
from wzgram.handlers import RawUpdateHandler
from wzgram.types import Message, User

from summon.bot.handlers import register_handlers
from summon.config import Settings
from summon.models import GroupSettings

NOW = datetime(2026, 11, 6, tzinfo=UTC)


def rule(*, enabled=True, trigger="hello", response="<hello & welcome>"):
    return {
        "trigger": trigger,
        "response": response,
        "enabled": enabled,
        "revision": "a" * 32,
        "created_at": NOW,
        "created_by": 42,
        "updated_at": NOW,
        "updated_by": 42,
    }


async def setup(*, enabled=True, trigger="hello", **settings):
    group = {
        "settings": GroupSettings(**settings).model_dump(),
        "auto_replies_v1": {"welcome": rule(enabled=enabled, trigger=trigger)},
    }
    store = SimpleNamespace(
        groups=SimpleNamespace(
            db=object(),
            get=AsyncMock(return_value=group),
            ensure=AsyncMock(),
            record_spam=AsyncMock(),
        ),
        collection=SimpleNamespace(
            record_message=AsyncMock(return_value={"settings": {"spawn_enabled": False}})
        ),
        catalog=SimpleNamespace(choose=AsyncMock()),
        trades=object(),
        gifts=object(),
        limiter=SimpleNamespace(
            allow=AsyncMock(return_value=True), claim_once=AsyncMock(return_value=True)
        ),
    )
    app = Client(
        "offline-test",
        api_id=123,
        api_hash="test",
        bot_token="123:fake",
        in_memory=True,
        workers=1,
        skip_updates=True,
        fetch_replies=False,
    )
    app.me = User(id=123, is_bot=True, username="summon_test", first_name="Bot")
    register_handlers(
        app,
        store,
        Settings(bot_api_rich_enabled=False, premium_emoji_enabled=False, _env_file=None),
    )
    app._test_complete = asyncio.Queue()

    async def after_update(_client, update, _users, _chats):
        if isinstance(update, raw.types.UpdateUserStatus):
            app._test_complete.put_nowait(None)

    app.add_handler(RawUpdateHandler(after_update), group=100)
    await asyncio.sleep(0)
    await app.dispatcher.start()
    return app, store, group


def update(_bot, text="hello", *, edited=False, chat_type="supergroup", **extra):
    now = int(datetime.now(UTC).timestamp())
    author = extra.get("from", {"id": 42, "is_bot": False, "first_name": "Member"})
    actor = raw.types.User(
        id=author["id"],
        first_name=author.get("first_name", "Member"),
        bot=author.get("is_bot", False),
        access_hash=author["id"],
        usernames=[],
        restriction_reason=[],
    )
    users = {
        author["id"]: actor,
        99: raw.types.User(
            id=99,
            first_name="Inline",
            bot=True,
            access_hash=99,
            usernames=[],
            restriction_reason=[],
        ),
    }
    group = raw.types.Chat(
        id=100,
        title="Test",
        photo=raw.types.ChatPhotoEmpty(),
        participants_count=2,
        date=now,
        version=1,
    )
    chats = {100: group}
    peer = (
        raw.types.PeerUser(user_id=author["id"])
        if chat_type == "private"
        else raw.types.PeerChat(chat_id=100)
    )
    sender = raw.types.PeerUser(user_id=author["id"])
    if extra.get("sender_chat"):
        sender = raw.types.PeerChannel(channel_id=200)
        chats[200] = raw.types.Channel(
            id=200,
            title="Channel",
            photo=raw.types.ChatPhotoEmpty(),
            date=now,
            broadcast=True,
            access_hash=200,
            usernames=[],
            restriction_reason=[],
        )
    message_id = extra.get("message_id", 123)
    if extra.get("ephemeral_message_id"):
        message = raw.types.EphemeralMessage(
            id=extra["ephemeral_message_id"],
            from_id=sender,
            receiver_id=123,
            date=now,
            message=text,
            peer_id=peer,
            entities=[],
        )
        return raw.types.UpdateNewEphemeralMessage(message=message), users, chats
    forward_keys = {
        "forward_date",
        "forward_from_message_id",
        "forward_signature",
        "forward_origin",
    }
    forward = (
        raw.types.MessageFwdHeader(date=now, from_name="Forwarded")
        if forward_keys.intersection(extra)
        else None
    )
    thread_id = extra.get("message_thread_id")
    reply = (
        raw.types.MessageReplyHeader(
            forum_topic=True, reply_to_msg_id=thread_id, reply_to_top_id=thread_id
        )
        if thread_id
        else None
    )
    message = raw.types.Message(
        id=message_id,
        peer_id=peer,
        from_id=sender,
        date=now,
        message=text,
        entities=[],
        restriction_reason=[],
        fwd_from=forward,
        reply_to=reply,
        via_bot_id=99 if extra.get("via_bot") else None,
        via_business_bot_id=99 if extra.get("sender_business_bot") else None,
    )
    kind = raw.types.UpdateEditMessage if edited else raw.types.UpdateNewMessage
    return kind(message=message, pts=1, pts_count=1), users, chats


async def dispatch(app, packet):
    await app.dispatcher.updates_queue.put(packet)
    marker = raw.types.UpdateUserStatus(user_id=42, status=raw.types.UserStatusRecently())
    await app.dispatcher.updates_queue.put((marker, {}, {}))
    await asyncio.wait_for(app._test_complete.get(), 2)


async def test_exact_reply_is_public_escaped_rate_limited_and_activity_still_counts():
    dp, store, _ = await setup()
    bot = dp
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(dp, update(bot, "  ＨＥＬＬＯ  ", message_thread_id=11))
    send.assert_awaited_once()
    assert send.await_args.args[:2] == (bot, -100)
    card = send.await_args.args[2]
    assert "&lt;hello &amp; welcome&gt;" in card.telegram_html(premium=False)
    assert "<hello" not in card.telegram_html(premium=False)
    assert send.await_args.kwargs["rich"] is False
    assert send.await_args.kwargs["message_thread_id"] == 11
    assert store.limiter.allow.await_count == 2
    assert any(
        call.args[0] == "group:-100:auto-reply:user:42"
        for call in store.limiter.allow.await_args_list
    )
    assert any(
        call.args[0] == "group:-100:auto-reply" for call in store.limiter.allow.await_args_list
    )
    store.collection.record_message.assert_awaited_once()
    await bot.dispatcher.stop()


@pytest.mark.parametrize(
    "text,extra",
    [
        ("say hello", {}),
        ("hello please", {}),
        ("/hello", {}),
        ("hello", {"ephemeral_message_id": 99}),
        ("hello", {"from": {"id": 42, "is_bot": True, "first_name": "Bot"}}),
        ("hello", {"sender_chat": {"id": -200, "type": "channel", "title": "Channel"}}),
        ("hello", {"message_id": 0}),
        ("hello", {"forward_date": int(datetime.now(UTC).timestamp())}),
        ("hello", {"forward_from_message_id": 99}),
        ("hello", {"forward_signature": "forwarded"}),
        ("hello", {"via_bot": {"id": 99, "is_bot": True, "first_name": "Inline"}}),
        ("hello", {"sender_business_bot": {"id": 99, "is_bot": True, "first_name": "Business"}}),
        (
            "hello",
            {
                "forward_origin": {
                    "type": "hidden_user",
                    "date": int(datetime.now(UTC).timestamp()),
                    "sender_user_name": "A",
                }
            },
        ),
    ],
)
async def test_no_auto_reply_for_partial_commands_ephemeral_bots_anonymous_or_forwarded(
    text, extra
):
    dp, store, _ = await setup()
    bot = dp
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(dp, update(bot, text, **extra))
    send.assert_not_awaited()
    store.limiter.allow.assert_not_awaited()
    await bot.dispatcher.stop()


async def test_no_auto_reply_for_edits_or_private_chat():
    dp, store, _ = await setup()
    bot = dp
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(dp, update(bot, edited=True))
        await dispatch(dp, update(bot, chat_type="private"))
    send.assert_not_awaited()
    store.limiter.allow.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()
    await bot.dispatcher.stop()


async def test_disabled_or_missing_group_rules_do_not_send_or_consume_budget():
    dp, store, _ = await setup(enabled=False)
    bot = dp
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(dp, update(bot))
    send.assert_not_awaited()
    store.limiter.allow.assert_not_awaited()
    store.collection.record_message.assert_awaited_once()
    await bot.dispatcher.stop()


@pytest.mark.parametrize("decisions", [[False], [True, False]])
async def test_rate_limited_matches_do_not_send_but_still_count_activity(decisions):
    dp, store, _ = await setup()
    store.limiter.allow.side_effect = decisions
    bot = dp
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(dp, update(bot))
    send.assert_not_awaited()
    assert store.limiter.allow.await_count == len(decisions)
    store.collection.record_message.assert_awaited_once()
    await bot.dispatcher.stop()


async def test_spam_protection_runs_before_auto_reply_and_spawns():
    dp, store, _ = await setup(trigger="go", blocked_keywords=["go"])
    bot = dp
    with (
        patch.object(Client, "get_chat_member", new_callable=AsyncMock) as member,
        patch.object(Message, "delete", new_callable=AsyncMock, return_value=True) as delete,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
    ):
        member.side_effect = [
            SimpleNamespace(status="member"),
            SimpleNamespace(
                status="administrator", privileges=SimpleNamespace(can_delete_messages=True)
            ),
        ]
        await dispatch(dp, update(bot, "go"))
    delete.assert_awaited_once()
    store.groups.record_spam.assert_awaited_once()
    send.assert_not_awaited()
    store.limiter.allow.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()
    await bot.dispatcher.stop()


async def test_corrupt_rules_and_ambiguous_send_failure_do_not_break_activity(caplog):
    dp, store, group = await setup()
    bot = dp
    group["auto_replies_v1"] = {"bad": {"response": "SECRET_VALUE"}}
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(dp, update(bot))
    send.assert_not_awaited()
    store.collection.record_message.assert_awaited_once()
    assert "SECRET_VALUE" not in caplog.text
    await bot.dispatcher.stop()

    dp, store, _ = await setup()
    bot = dp
    error = ConnectionError("delivery uncertain")
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock, side_effect=error) as send:
        await dispatch(dp, update(bot))
    send.assert_awaited_once()  # Never retry an ambiguous send.
    store.collection.record_message.assert_awaited_once()
    await bot.dispatcher.stop()


async def test_duplicate_updates_attempt_public_delivery_at_most_once():
    dp, store, _ = await setup()
    claims = set()

    async def claim_once(key, *, seconds):
        assert seconds == 48 * 60 * 60
        if key in claims:
            return False
        claims.add(key)
        return True

    store.limiter.claim_once = AsyncMock(side_effect=claim_once)
    bot = dp
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(dp, update(bot))
        await dispatch(dp, update(bot))
    send.assert_awaited_once()
    assert store.limiter.claim_once.await_count == 2
    assert store.collection.record_message.await_count == 2  # Existing collection semantics.
    assert len(claims) == 1
    await bot.dispatcher.stop()


async def test_auto_reply_and_spawn_delivery_coexist_in_the_same_topic():
    with patch("summon.bot.handlers.send_photo_card", new_callable=AsyncMock) as spawn:
        dp, store, _ = await setup()
        await _assert_auto_reply_and_spawn(dp, store, spawn)


async def _assert_auto_reply_and_spawn(dp, store, spawn):
    store.collection.record_message.return_value = {
        "settings": {"spawn_enabled": True, "spawn_every": 20},
        "message_count": 20,
    }
    store.collection.reserve_spawn = AsyncMock(return_value={"id": "spawn-one"})
    store.collection.mark_delivery_failed = AsyncMock()
    store.catalog.choose.return_value = {
        "_id": "aiko",
        "category": "Sky",
        "rarity": "rare",
        "image_url": "https://example.com/aiko.webp",
    }
    bot = dp
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as reply:
        await dispatch(dp, update(bot, message_thread_id=11))
    reply.assert_awaited_once()
    spawn.assert_awaited_once()
    assert spawn.await_args.kwargs["message_thread_id"] == 11
    store.collection.record_message.assert_awaited_once()
    store.groups.get.assert_awaited_once()  # Reuse the protection snapshot.
    await bot.dispatcher.stop()


async def test_known_and_unknown_commands_never_trigger_a_public_auto_reply():
    dp, store, _ = await setup()
    bot = dp
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(dp, update(bot, "/help"))
        await dispatch(dp, update(bot, "/hello"))
    assert send.await_count == 1
    assert send.await_args.args[2].title == "Your SUMMON guide"
    store.limiter.allow.assert_not_awaited()
    await bot.dispatcher.stop()


async def test_ambiguous_telegram_delivery_is_not_retried_when_update_replays():
    dp, store, _ = await setup()
    claimed = set()

    async def claim_once(key, *, seconds):
        if key in claimed:
            return False
        claimed.add(key)
        return True

    store.limiter.claim_once.side_effect = claim_once
    bot = dp
    error = ConnectionError("timeout")
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock, side_effect=error) as send:
        await dispatch(dp, update(bot))
        await dispatch(dp, update(bot))
    send.assert_awaited_once()
    assert store.collection.record_message.await_count == 2
    await bot.dispatcher.stop()


async def test_shared_per_user_and_group_fixed_windows_bound_deliveries():
    dp, store, _ = await setup()
    buckets = {}
    claims = set()

    async def allow(key, *, limit, seconds):
        assert seconds == 60
        buckets[key] = buckets.get(key, 0) + 1
        return buckets[key] <= limit

    async def claim_once(key, *, seconds):
        assert seconds == 172800
        if key in claims:
            return False
        claims.add(key)
        return True

    store.limiter.allow.side_effect = allow
    store.limiter.claim_once.side_effect = claim_once
    bot = dp
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        # The same member can receive only three per fixed minute.
        for message_id in range(1, 5):
            await dispatch(dp, update(bot, message_id=message_id))
        assert send.await_count == 3
        # Many distinct members cannot exceed the shared 15-per-group budget.
        for user_id in range(100, 120):
            await dispatch(
                dp,
                update(
                    bot,
                    message_id=user_id,
                    **{"from": {"id": user_id, "is_bot": False, "first_name": "Member"}},
                ),
            )
        assert send.await_count == 15
    assert store.collection.record_message.await_count == 24
    await bot.dispatcher.stop()


async def test_database_claim_failure_fails_closed_without_blocking_collection():
    from pymongo.errors import AutoReconnect

    dp, store, _ = await setup()
    store.limiter.claim_once.side_effect = AutoReconnect("database error")
    bot = dp
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(dp, update(bot))
    send.assert_not_awaited()
    store.collection.record_message.assert_awaited_once()
    await bot.dispatcher.stop()
