"""Protection must run before group commands and on delivered edits."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.types import Message, Update

from summon.bot.handlers import build_router
from summon.config import Settings
from summon.models import GroupSettings


def group_store(**settings):
    groups = SimpleNamespace(
        db=object(),
        get=AsyncMock(return_value={"settings": GroupSettings(**settings).model_dump()}),
        record_spam=AsyncMock(),
        ensure=AsyncMock(),
    )
    return SimpleNamespace(
        groups=groups,
        collection=SimpleNamespace(record_message=AsyncMock()),
        catalog=object(),
        trades=object(),
        gifts=object(),
    )


def update(bot, *, text, edited=False, chat_type="supergroup", ephemeral_id=None):
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
    }
    if text.startswith("/help"):
        body["entities"] = [{"type": "bot_command", "offset": 0, "length": 5}]
    if ephemeral_id is not None:
        body["ephemeral_message_id"] = ephemeral_id
    return Update.model_validate(
        {"update_id": 1, "edited_message" if edited else "message": body}, context={"bot": bot}
    )


def dispatcher(store):
    dp = Dispatcher()
    dp.include_router(
        build_router(
            store, Settings(bot_api_rich_enabled=False, premium_emoji_enabled=False, _env_file=None)
        )
    )
    return dp


async def test_protection_blocks_command_before_handler_and_audits_confirmed_deletion():
    store = group_store(link_filter=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    with (
        patch.object(Bot, "get_chat_member", new_callable=AsyncMock) as member,
        patch.object(Message, "delete", new_callable=AsyncMock) as delete,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
    ):
        member.side_effect = [
            SimpleNamespace(status="member"),
            SimpleNamespace(status="administrator", can_delete_messages=True),
        ]
        await dp.feed_update(bot, update(bot, text="/help https://example.com"))
    delete.assert_awaited_once()
    store.groups.record_spam.assert_awaited_once()
    assert store.groups.record_spam.await_args.args[:3] == (-100, 42, "link")
    send.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()
    await bot.session.close()


async def test_administrators_are_exempt_and_the_command_still_runs():
    store = group_store(link_filter=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    with (
        patch.object(
            Bot,
            "get_chat_member",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(status="administrator"),
        ),
        patch.object(Message, "delete", new_callable=AsyncMock) as delete,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
    ):
        await dp.feed_update(bot, update(bot, text="/help https://example.com"))
    delete.assert_not_awaited()
    store.groups.record_spam.assert_not_awaited()
    send.assert_awaited_once()
    await bot.session.close()


async def test_suspected_command_is_not_executed_if_bot_cannot_delete():
    store = group_store(link_filter=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    with (
        patch.object(Bot, "get_chat_member", new_callable=AsyncMock) as member,
        patch.object(Message, "delete", new_callable=AsyncMock) as delete,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
    ):
        member.side_effect = [
            SimpleNamespace(status="member"),
            SimpleNamespace(status="administrator", can_delete_messages=False),
        ]
        await dp.feed_update(bot, update(bot, text="/help https://example.com"))
    delete.assert_not_awaited()
    store.groups.record_spam.assert_not_awaited()
    send.assert_not_awaited()
    await bot.session.close()


async def test_edited_messages_are_inspected_but_never_count_as_activity():
    store = group_store(link_filter=True, flood_enabled=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    with (
        patch.object(Bot, "get_chat_member", new_callable=AsyncMock) as member,
        patch.object(Message, "delete", new_callable=AsyncMock) as delete,
    ):
        member.side_effect = [
            SimpleNamespace(status="member"),
            SimpleNamespace(status="administrator", can_delete_messages=True),
        ]
        await dp.feed_update(bot, update(bot, text="https://example.com", edited=True))
    assert "edited_message" in dp.resolve_used_update_types()
    delete.assert_awaited_once()
    store.groups.record_spam.assert_awaited_once()
    store.collection.record_message.assert_not_awaited()
    await bot.session.close()


async def test_private_messages_do_not_use_group_protection():
    store = group_store(link_filter=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dp.feed_update(
            bot, update(bot, text="/help https://example.com", chat_type="private")
        )
    store.groups.get.assert_not_awaited()
    send.assert_awaited_once()
    await bot.session.close()


async def test_suspected_ephemeral_command_is_not_deleted_as_an_ordinary_message():
    store = group_store(link_filter=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    with (
        patch.object(
            Bot,
            "get_chat_member",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(status="member"),
        ) as member,
        patch.object(Message, "delete", new_callable=AsyncMock) as delete,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
    ):
        await dp.feed_update(bot, update(bot, text="/help https://example.com", ephemeral_id=999))
    member.assert_awaited_once_with(-100, 42)
    delete.assert_not_awaited()
    store.groups.record_spam.assert_not_awaited()
    send.assert_not_awaited()
    await bot.session.close()


async def test_new_chat_activity_is_checked_and_counted_only_once():
    store = group_store(flood_enabled=True)
    counter = AsyncMock(return_value={"count": 1})
    store.groups.db = SimpleNamespace(rate_limits=SimpleNamespace(find_one_and_update=counter))
    store.collection.record_message.return_value = {"settings": {"spawn_enabled": False}}
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    with patch.object(Bot, "get_chat_member", new_callable=AsyncMock) as member:
        await dp.feed_update(bot, update(bot, text="A normal conversation"))
    counter.assert_awaited_once()
    store.groups.ensure.assert_awaited_once()
    store.collection.record_message.assert_awaited_once()
    member.assert_not_awaited()
    await bot.session.close()


async def test_safe_edit_does_not_count_flood_or_spawn_and_does_not_run_command():
    store = group_store(flood_enabled=True, duplicate_enabled=True)
    counter = AsyncMock(return_value={"count": 1})
    store.groups.db = SimpleNamespace(rate_limits=SimpleNamespace(find_one_and_update=counter))
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dp.feed_update(bot, update(bot, text="/help", edited=True))
    counter.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()
    store.groups.ensure.assert_not_awaited()
    send.assert_not_awaited()
    await bot.session.close()


async def test_join_service_event_bypasses_spam_and_still_welcomes():
    store = group_store(welcome_enabled=True, link_filter=True, flood_enabled=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    raw = {
        "update_id": 99,
        "message": {
            "message_id": 2,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": -100, "type": "supergroup", "title": "Test"},
            "from": {"id": 42, "is_bot": False, "first_name": "Admin"},
            "new_chat_members": [{"id": 43, "is_bot": False, "first_name": "New"}],
        },
    }
    with (
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
        patch.object(Bot, "get_chat_member", new_callable=AsyncMock) as member,
    ):
        await dp.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))
    # The welcome handler reads settings once; the middleware must not read them.
    store.groups.get.assert_awaited_once_with(-100)
    member.assert_not_awaited()
    send.assert_awaited_once()
    store.collection.record_message.assert_not_awaited()
    await bot.session.close()


async def test_edited_caption_hidden_link_is_checked():
    store = group_store(link_filter=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    raw = {
        "update_id": 2,
        "edited_message": {
            "message_id": 3,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": -100, "type": "supergroup", "title": "Test"},
            "from": {"id": 42, "is_bot": False, "first_name": "Member"},
            "photo": [{"file_id": "photo", "file_unique_id": "unique", "width": 1, "height": 1}],
            "caption": "click here",
            "caption_entities": [
                {"type": "text_link", "offset": 0, "length": 5, "url": "https://example.com"}
            ],
        },
    }
    with (
        patch.object(Bot, "get_chat_member", new_callable=AsyncMock) as member,
        patch.object(Message, "delete", new_callable=AsyncMock, return_value=True) as delete,
    ):
        member.side_effect = [
            SimpleNamespace(status="member"),
            SimpleNamespace(status="administrator", can_delete_messages=True),
        ]
        await dp.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))
    delete.assert_awaited_once()
    assert store.groups.record_spam.await_args.args[:3] == (-100, 42, "link")
    store.collection.record_message.assert_not_awaited()
    await bot.session.close()


async def test_unverified_admin_status_suppresses_flagged_command_without_deletion():
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import GetChatMember

    store = group_store(link_filter=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    error = TelegramNetworkError(
        method=GetChatMember(chat_id=-100, user_id=42), message="unavailable"
    )
    with (
        patch.object(Bot, "get_chat_member", new_callable=AsyncMock, side_effect=error),
        patch.object(Message, "delete", new_callable=AsyncMock) as delete,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
    ):
        await dp.feed_update(bot, update(bot, text="/help https://example.com"))
    delete.assert_not_awaited()
    store.groups.record_spam.assert_not_awaited()
    send.assert_not_awaited()
    await bot.session.close()


async def test_uncertain_delete_does_not_create_false_audit_or_run_command():
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import DeleteMessage

    store = group_store(link_filter=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    error = TelegramNetworkError(
        method=DeleteMessage(chat_id=-100, message_id=123), message="unavailable"
    )
    with (
        patch.object(Bot, "get_chat_member", new_callable=AsyncMock) as member,
        patch.object(Message, "delete", new_callable=AsyncMock, side_effect=error) as delete,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
    ):
        member.side_effect = [
            SimpleNamespace(status="member"),
            SimpleNamespace(status="administrator", can_delete_messages=True),
        ]
        await dp.feed_update(bot, update(bot, text="/help https://example.com"))
    delete.assert_awaited_once()  # Never retry an ambiguous Telegram result.
    store.groups.record_spam.assert_not_awaited()
    send.assert_not_awaited()
    await bot.session.close()


async def test_false_delete_result_does_not_create_false_audit():
    store = group_store(link_filter=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    with (
        patch.object(Bot, "get_chat_member", new_callable=AsyncMock) as member,
        patch.object(Message, "delete", new_callable=AsyncMock, return_value=False) as delete,
    ):
        member.side_effect = [
            SimpleNamespace(status="member"),
            SimpleNamespace(status="administrator", can_delete_messages=True),
        ]
        await dp.feed_update(bot, update(bot, text="/help https://example.com"))
    delete.assert_awaited_once()
    store.groups.record_spam.assert_not_awaited()
    await bot.session.close()


async def test_flooding_a_command_is_checked_once_before_the_command_handler():
    store = group_store(flood_enabled=True, flood_limit=3)
    counter = AsyncMock(return_value={"count": 4})
    store.groups.db = SimpleNamespace(rate_limits=SimpleNamespace(find_one_and_update=counter))
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    with (
        patch.object(Bot, "get_chat_member", new_callable=AsyncMock) as member,
        patch.object(Message, "delete", new_callable=AsyncMock, return_value=True) as delete,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
    ):
        member.side_effect = [
            SimpleNamespace(status="member"),
            SimpleNamespace(status="administrator", can_delete_messages=True),
        ]
        await dp.feed_update(bot, update(bot, text="/help"))
    counter.assert_awaited_once()
    delete.assert_awaited_once()
    assert store.groups.record_spam.await_args.args[:3] == (-100, 42, "flood")
    send.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()
    await bot.session.close()


async def test_other_group_service_events_do_not_count_as_flood_or_spawns():
    store = group_store(flood_enabled=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    date = int(datetime.now(UTC).timestamp())
    raw = {
        "update_id": 3,
        "message": {
            "message_id": 9,
            "date": date,
            "chat": {"id": -100, "type": "supergroup", "title": "Test"},
            "from": {"id": 42, "is_bot": False, "first_name": "Member"},
            "pinned_message": {
                "message_id": 5,
                "date": date,
                "chat": {"id": -100, "type": "supergroup", "title": "Test"},
                "text": "Pinned message",
            },
        },
    }
    await dp.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))
    store.groups.get.assert_not_awaited()
    store.groups.ensure.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()
    await bot.session.close()


async def test_sender_chat_message_is_not_deleted_without_a_checkable_human_role():
    store = group_store(link_filter=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    raw = {
        "update_id": 4,
        "message": {
            "message_id": 10,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": -100, "type": "supergroup", "title": "Test"},
            "from": {"id": 42, "is_bot": False, "first_name": "Sender"},
            "sender_chat": {"id": -200, "type": "channel", "title": "Channel"},
            "text": "https://example.com",
        },
    }
    with patch.object(Message, "delete", new_callable=AsyncMock) as delete:
        await dp.feed_update(bot, Update.model_validate(raw, context={"bot": bot}))
    store.groups.get.assert_not_awaited()
    store.groups.ensure.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()
    delete.assert_not_awaited()
    await bot.session.close()


async def test_flagged_ordinary_message_never_advances_spawn_activity():
    store = group_store(link_filter=True)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = dispatcher(store)
    with (
        patch.object(Bot, "get_chat_member", new_callable=AsyncMock) as member,
        patch.object(Message, "delete", new_callable=AsyncMock, return_value=True) as delete,
    ):
        member.side_effect = [
            SimpleNamespace(status="member"),
            SimpleNamespace(status="administrator", can_delete_messages=True),
        ]
        await dp.feed_update(bot, update(bot, text="https://example.com"))
    delete.assert_awaited_once()
    store.groups.record_spam.assert_awaited_once()
    store.groups.ensure.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()
    await bot.session.close()
