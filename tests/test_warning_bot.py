"""Warning review commands reauthorize, scope targets and keep responses private."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetChatMember
from pymongo.errors import AutoReconnect

from summon.bot.handlers import build_router
from summon.config import Settings

WARNING_ID = "a" * 32
CURSOR = "b" * 32
NOW = datetime(2026, 11, 5, tzinfo=UTC)


def setup():
    groups = SimpleNamespace(
        db=object(),
        list_warnings=AsyncMock(
            return_value={
                "items": [
                    {
                        "id": WARNING_ID,
                        "actor_id": 44,
                        "reason": "<private reason>",
                        "created_at": NOW,
                    }
                ],
                "active_count": 6,
                "next_cursor": CURSOR,
            }
        ),
        revoke_warning=AsyncMock(return_value={"id": WARNING_ID, "status": "revoked"}),
    )
    store = SimpleNamespace(
        collection=object(),
        catalog=object(),
        trades=object(),
        gifts=object(),
        groups=groups,
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
    )
    router = build_router(store, Settings(_env_file=None))
    bot = SimpleNamespace(
        id=99,
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status="administrator", can_restrict_members=True)
        ),
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(
            id=42, is_bot=False, first_name="Actor", last_name=None, username=None
        ),
        sender_chat=None,
        chat=SimpleNamespace(id=-100, type="supergroup", title="Test"),
        reply_to_message=SimpleNamespace(
            from_user=SimpleNamespace(id=55, is_bot=False, first_name="Target"), sender_chat=None
        ),
        message_thread_id=None,
        ephemeral_message_id=None,
        bot=bot,
    )
    return router, store, bot, message


def handler(router, name):
    return next(item.callback for item in router.message.handlers if item.callback.__name__ == name)


async def test_warning_list_checks_live_actor_and_sends_sensitive_result_privately():
    router, store, bot, message = setup()
    with (
        patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as public,
    ):
        await handler(router, "review_warnings")(message, SimpleNamespace(args=None))
    bot.get_chat_member.assert_awaited_once_with(-100, 42)
    store.groups.list_warnings.assert_awaited_once_with(-100, 55, before=None, limit=5)
    private.assert_awaited_once()
    public.assert_not_awaited()
    card = private.await_args.args[3]
    assert WARNING_ID in card.telegram_html(premium=False)
    assert "&lt;private reason&gt;" in card.telegram_html(premium=False)
    assert CURSOR in card.telegram_html(premium=False)
    assert store.limiter.allow.await_args.args == ("user:42:warning_review",)


async def test_warning_list_cursor_is_bounded_and_role_rechecked_on_every_page():
    router, store, bot, message = setup()
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock):
        await handler(router, "review_warnings")(message, SimpleNamespace(args=CURSOR))
        bot.get_chat_member.return_value = SimpleNamespace(
            status="administrator", can_restrict_members=False
        )
        await handler(router, "review_warnings")(message, SimpleNamespace(args=CURSOR))
    assert store.groups.list_warnings.await_count == 1
    assert store.groups.list_warnings.await_args.kwargs == {"before": CURSOR, "limit": 5}
    assert bot.get_chat_member.await_count == 2


@pytest.mark.parametrize("args", [None, WARNING_ID, f"{WARNING_ID} yes", f"{'x' * 32} confirm"])
async def test_unwarn_requires_explicit_valid_id_and_confirm_keyword(args):
    router, store, bot, message = setup()
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock):
        await handler(router, "unwarn")(message, SimpleNamespace(args=args))
    store.groups.revoke_warning.assert_not_awaited()
    bot.get_chat_member.assert_not_awaited()


async def test_unwarn_uses_replied_target_current_actor_and_private_confirmation():
    router, store, bot, message = setup()
    with (
        patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as public,
    ):
        await handler(router, "unwarn")(
            message, SimpleNamespace(args=f"{WARNING_ID} confirm Recorded in error")
        )
    bot.get_chat_member.assert_awaited_once_with(-100, 42)
    assert store.groups.revoke_warning.await_args.args[:5] == (
        -100,
        42,
        55,
        WARNING_ID,
        "Recorded in error",
    )
    assert isinstance(store.groups.revoke_warning.await_args.args[5], datetime)
    private.assert_awaited_once()
    public.assert_not_awaited()


async def test_unwarn_denied_for_lost_permission_or_rate_limit():
    router, store, bot, message = setup()
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock):
        bot.get_chat_member.return_value = SimpleNamespace(status="member")
        await handler(router, "unwarn")(message, SimpleNamespace(args=f"{WARNING_ID} confirm"))
        store.limiter.allow.return_value = False
        await handler(router, "unwarn")(message, SimpleNamespace(args=f"{WARNING_ID} confirm"))
    bot.get_chat_member.assert_awaited_once()
    store.groups.revoke_warning.assert_not_awaited()


async def test_reply_to_sender_chat_is_not_treated_as_a_user_target():
    router, store, bot, message = setup()
    message.reply_to_message.sender_chat = SimpleNamespace(id=-200)
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock):
        await handler(router, "unwarn")(message, SimpleNamespace(args=f"{WARNING_ID} confirm"))
        await handler(router, "review_warnings")(message, SimpleNamespace(args=None))
    bot.get_chat_member.assert_not_awaited()
    store.groups.revoke_warning.assert_not_awaited()
    store.groups.list_warnings.assert_not_awaited()


async def test_telegram_lookup_error_is_sanitized_and_fails_closed():
    router, store, bot, message = setup()
    bot.get_chat_member.side_effect = TelegramNetworkError(
        method=GetChatMember(chat_id=-100, user_id=42), message="secret network details"
    )
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private:
        await handler(router, "review_warnings")(message, SimpleNamespace(args=None))
    assert "secret network details" not in private.await_args.args[3].telegram_html(premium=False)
    store.groups.list_warnings.assert_not_awaited()


async def test_uncertain_revoke_is_not_retried_and_tells_admin_to_review():
    router, store, bot, message = setup()
    store.groups.revoke_warning.side_effect = AutoReconnect("sensitive connection details")
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private:
        await handler(router, "unwarn")(message, SimpleNamespace(args=f"{WARNING_ID} confirm"))
    store.groups.revoke_warning.assert_awaited_once()
    text = private.await_args.args[3].telegram_html(premium=False)
    assert "review" in text.lower()
    assert "sensitive connection details" not in text


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/warnings", "list"),
        (f"/unwarn {WARNING_ID} confirm Error", "revoke"),
    ],
)
async def test_aiogram_dispatches_each_warning_command_once(text, expected):
    from aiogram import Bot, Dispatcher
    from aiogram.types import Update

    router, store, _, _ = setup()
    store.groups.get = AsyncMock(return_value=None)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = Dispatcher()
    dp.include_router(router)
    command = text.split()[0]
    raw = {
        "update_id": 123,
        "message": {
            "message_id": 10,
            "date": int(NOW.timestamp()),
            "chat": {"id": -100, "type": "supergroup", "title": "Test"},
            "from": {"id": 42, "is_bot": False, "first_name": "Actor"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(command)}],
            "reply_to_message": {
                "message_id": 9,
                "date": int(NOW.timestamp()),
                "chat": {"id": -100, "type": "supergroup", "title": "Test"},
                "from": {"id": 55, "is_bot": False, "first_name": "Target"},
                "text": "Hello",
            },
        },
    }
    update = Update.model_validate(raw, context={"bot": bot})
    with (
        patch.object(
            Bot,
            "get_chat_member",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(status="creator"),
        ),
        patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private,
    ):
        await dp.feed_update(bot, update)
    private.assert_awaited_once()
    assert store.groups.list_warnings.await_count == (1 if expected == "list" else 0)
    assert store.groups.revoke_warning.await_count == (1 if expected == "revoke" else 0)
    await bot.session.close()


async def test_warning_review_uses_summon_alert_without_making_content_public():
    from summon.bot.presentation import Emoji

    router, _, _, message = setup()
    with (
        patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as public,
    ):
        await handler(router, "review_warnings")(message, SimpleNamespace(args=None))
    assert private.await_args.args[3].emoji is Emoji.ALERT
    public.assert_not_awaited()
