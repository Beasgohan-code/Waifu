"""Exact-match auto replies must not bypass protection, privacy or spawn activity."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.types import Message, Update

from summon.bot.handlers import build_router
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


def setup(*, enabled=True, trigger="hello", **settings):
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
    dp = Dispatcher()
    dp.include_router(
        build_router(
            store, Settings(bot_api_rich_enabled=False, premium_emoji_enabled=False, _env_file=None)
        )
    )
    return dp, store, group


def update(bot, text="hello", *, edited=False, chat_type="supergroup", **extra):
    body = {
        "message_id": 123,
        "date": int(datetime.now(UTC).timestamp()),
        "chat": {
            "id": -100 if chat_type != "private" else 42,
            "type": chat_type,
            **({"title": "Test"} if chat_type != "private" else {"first_name": "Member"}),
        },
        "from": {"id": 42, "is_bot": False, "first_name": "Member"},
        "text": text,
        **extra,
    }
    return Update.model_validate(
        {"update_id": 1, "edited_message" if edited else "message": body}, context={"bot": bot}
    )


async def test_exact_reply_is_public_escaped_rate_limited_and_activity_still_counts():
    dp, store, _ = setup()
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dp.feed_update(bot, update(bot, "  ＨＥＬＬＯ  ", message_thread_id=11))
    send.assert_awaited_once()
    assert send.await_args.args[:2] == (bot, -100)
    card = send.await_args.args[2]
    assert "&lt;hello &amp; welcome&gt;" in card.telegram_html(premium=False)
    assert "<hello" not in card.telegram_html(premium=False)
    assert send.await_args.kwargs["bot_api_rich"] is False
    assert send.await_args.kwargs["rich_client"] is None
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
    await bot.session.close()


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
    dp, store, _ = setup()
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dp.feed_update(bot, update(bot, text, **extra))
    send.assert_not_awaited()
    store.limiter.allow.assert_not_awaited()
    await bot.session.close()


async def test_no_auto_reply_for_edits_or_private_chat():
    dp, store, _ = setup()
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dp.feed_update(bot, update(bot, edited=True))
        await dp.feed_update(bot, update(bot, chat_type="private"))
    send.assert_not_awaited()
    store.limiter.allow.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()
    await bot.session.close()


async def test_disabled_or_missing_group_rules_do_not_send_or_consume_budget():
    dp, store, _ = setup(enabled=False)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dp.feed_update(bot, update(bot))
    send.assert_not_awaited()
    store.limiter.allow.assert_not_awaited()
    store.collection.record_message.assert_awaited_once()
    await bot.session.close()


@pytest.mark.parametrize("decisions", [[False], [True, False]])
async def test_rate_limited_matches_do_not_send_but_still_count_activity(decisions):
    dp, store, _ = setup()
    store.limiter.allow.side_effect = decisions
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dp.feed_update(bot, update(bot))
    send.assert_not_awaited()
    assert store.limiter.allow.await_count == len(decisions)
    store.collection.record_message.assert_awaited_once()
    await bot.session.close()


async def test_spam_protection_runs_before_auto_reply_and_spawns():
    dp, store, _ = setup(trigger="go", blocked_keywords=["go"])
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with (
        patch.object(Bot, "get_chat_member", new_callable=AsyncMock) as member,
        patch.object(Message, "delete", new_callable=AsyncMock, return_value=True) as delete,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
    ):
        member.side_effect = [
            SimpleNamespace(status="member"),
            SimpleNamespace(status="administrator", can_delete_messages=True),
        ]
        await dp.feed_update(bot, update(bot, "go"))
    delete.assert_awaited_once()
    store.groups.record_spam.assert_awaited_once()
    send.assert_not_awaited()
    store.limiter.allow.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()
    await bot.session.close()


async def test_corrupt_rules_and_ambiguous_send_failure_do_not_break_activity(caplog):
    dp, store, group = setup()
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    group["auto_replies_v1"] = {"bad": {"response": "SECRET_VALUE"}}
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dp.feed_update(bot, update(bot))
    send.assert_not_awaited()
    store.collection.record_message.assert_awaited_once()
    assert "SECRET_VALUE" not in caplog.text
    await bot.session.close()

    dp, store, _ = setup()
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    error = TelegramNetworkError(
        method=SendMessage(chat_id=-100, text="x"), message="delivery uncertain"
    )
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock, side_effect=error) as send:
        await dp.feed_update(bot, update(bot))
    send.assert_awaited_once()  # Never retry an ambiguous send.
    store.collection.record_message.assert_awaited_once()
    await bot.session.close()


async def test_duplicate_updates_attempt_public_delivery_at_most_once():
    dp, store, _ = setup()
    claims = set()

    async def claim_once(key, *, seconds):
        assert seconds == 48 * 60 * 60
        if key in claims:
            return False
        claims.add(key)
        return True

    store.limiter.claim_once = AsyncMock(side_effect=claim_once)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dp.feed_update(bot, update(bot))
        await dp.feed_update(bot, update(bot))
    send.assert_awaited_once()
    assert store.limiter.claim_once.await_count == 2
    assert store.collection.record_message.await_count == 2  # Existing collection semantics.
    assert len(claims) == 1
    await bot.session.close()


async def test_auto_reply_and_spawn_delivery_coexist_in_the_same_topic():
    dp, store, _ = setup()
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
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with (
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as reply,
        patch("summon.bot.handlers.send_photo_card", new_callable=AsyncMock) as spawn,
    ):
        await dp.feed_update(bot, update(bot, message_thread_id=11))
    reply.assert_awaited_once()
    spawn.assert_awaited_once()
    assert spawn.await_args.kwargs["message_thread_id"] == 11
    store.collection.record_message.assert_awaited_once()
    store.groups.get.assert_awaited_once()  # Reuse the protection snapshot.
    await bot.session.close()


async def test_known_and_unknown_commands_never_trigger_a_public_auto_reply():
    dp, store, _ = setup()
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dp.feed_update(bot, update(bot, "/help"))
        await dp.feed_update(bot, update(bot, "/hello"))
    assert send.await_count == 1
    assert send.await_args.args[2].title == "Your SUMMON guide"
    store.limiter.allow.assert_not_awaited()
    await bot.session.close()


async def test_ambiguous_telegram_delivery_is_not_retried_when_update_replays():
    dp, store, _ = setup()
    claimed = set()

    async def claim_once(key, *, seconds):
        if key in claimed:
            return False
        claimed.add(key)
        return True

    store.limiter.claim_once.side_effect = claim_once
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    error = TelegramNetworkError(method=SendMessage(chat_id=-100, text="x"), message="timeout")
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock, side_effect=error) as send:
        await dp.feed_update(bot, update(bot))
        await dp.feed_update(bot, update(bot))
    send.assert_awaited_once()
    assert store.collection.record_message.await_count == 2
    await bot.session.close()


async def test_shared_per_user_and_group_fixed_windows_bound_deliveries():
    dp, store, _ = setup()
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
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        # The same member can receive only three per fixed minute.
        for message_id in range(1, 5):
            await dp.feed_update(bot, update(bot, message_id=message_id))
        assert send.await_count == 3
        # Many distinct members cannot exceed the shared 15-per-group budget.
        for user_id in range(100, 120):
            await dp.feed_update(
                bot,
                update(
                    bot,
                    message_id=user_id,
                    **{"from": {"id": user_id, "is_bot": False, "first_name": "Member"}},
                ),
            )
        assert send.await_count == 15
    assert store.collection.record_message.await_count == 24
    await bot.session.close()


async def test_database_claim_failure_fails_closed_without_blocking_collection():
    from pymongo.errors import AutoReconnect

    dp, store, _ = setup()
    store.limiter.claim_once.side_effect = AutoReconnect("database error")
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dp.feed_update(bot, update(bot))
    send.assert_not_awaited()
    store.collection.record_message.assert_awaited_once()
    await bot.session.close()
