"""Warning review commands reauthorize, scope targets and keep responses private."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pymongo.errors import AutoReconnect
from wzgram import Client, raw
from wzgram.enums import ChatType
from wzgram.types import Chat, Message, User

from summon.bot.handlers import register_handlers
from summon.config import Settings

WARNING_ID = "a" * 32
CURSOR = "b" * 32
NOW = datetime(2026, 11, 5, tzinfo=UTC)


async def setup():
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
    router = Client(
        "offline-test",
        api_id=123,
        api_hash="test",
        bot_token="123:fake",
        in_memory=True,
        workers=1,
        skip_updates=True,
    )
    router.me = User(id=99, is_bot=True, username="summon_test", first_name="Bot")
    router.get_chat_member = AsyncMock(
        return_value=SimpleNamespace(
            status="administrator", privileges=SimpleNamespace(can_restrict_members=True)
        )
    )
    bot = router
    register_handlers(router, store, Settings(_env_file=None))
    await asyncio.sleep(0)
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
        text="/test",
        caption=None,
        command=["test"],
    )
    return router, store, bot, message


def handler(router, name):
    callback = next(
        item.callback for item in router.dispatcher.groups[0] if item.callback.__name__ == name
    )

    async def invoke(message, args=None):
        command = {"review_warnings": "warnings"}.get(name, name)
        message.command = [command]
        argument = getattr(args, "args", None)
        message.text = "/" + command + (" " + argument if argument else "")
        return await callback(router, message)

    return invoke


async def test_warning_list_checks_live_actor_and_sends_sensitive_result_privately():
    router, store, bot, message = await setup()
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
    router, store, bot, message = await setup()
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock):
        await handler(router, "review_warnings")(message, SimpleNamespace(args=CURSOR))
        bot.get_chat_member.return_value = SimpleNamespace(
            status="administrator", privileges=SimpleNamespace(can_restrict_members=False)
        )
        await handler(router, "review_warnings")(message, SimpleNamespace(args=CURSOR))
    assert store.groups.list_warnings.await_count == 1
    assert store.groups.list_warnings.await_args.kwargs == {"before": CURSOR, "limit": 5}
    assert bot.get_chat_member.await_count == 2


@pytest.mark.parametrize("args", [None, WARNING_ID, f"{WARNING_ID} yes", f"{'x' * 32} confirm"])
async def test_unwarn_requires_explicit_valid_id_and_confirm_keyword(args):
    router, store, bot, message = await setup()
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock):
        await handler(router, "unwarn")(message, SimpleNamespace(args=args))
    store.groups.revoke_warning.assert_not_awaited()
    bot.get_chat_member.assert_not_awaited()


async def test_unwarn_uses_replied_target_current_actor_and_private_confirmation():
    router, store, bot, message = await setup()
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
    router, store, bot, message = await setup()
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock):
        bot.get_chat_member.return_value = SimpleNamespace(status="member")
        await handler(router, "unwarn")(message, SimpleNamespace(args=f"{WARNING_ID} confirm"))
        store.limiter.allow.return_value = False
        await handler(router, "unwarn")(message, SimpleNamespace(args=f"{WARNING_ID} confirm"))
    bot.get_chat_member.assert_awaited_once()
    store.groups.revoke_warning.assert_not_awaited()


async def test_reply_to_sender_chat_is_not_treated_as_a_user_target():
    router, store, bot, message = await setup()
    message.reply_to_message.sender_chat = SimpleNamespace(id=-200)
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock):
        await handler(router, "unwarn")(message, SimpleNamespace(args=f"{WARNING_ID} confirm"))
        await handler(router, "review_warnings")(message, SimpleNamespace(args=None))
    bot.get_chat_member.assert_not_awaited()
    store.groups.revoke_warning.assert_not_awaited()
    store.groups.list_warnings.assert_not_awaited()


async def test_telegram_lookup_error_is_sanitized_and_fails_closed():
    router, store, bot, message = await setup()
    bot.get_chat_member.side_effect = ConnectionError("secret network details")
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private:
        await handler(router, "review_warnings")(message, SimpleNamespace(args=None))
    assert "secret network details" not in private.await_args.args[3].telegram_html(premium=False)
    store.groups.list_warnings.assert_not_awaited()


async def test_uncertain_revoke_is_not_retried_and_tells_admin_to_review():
    router, store, bot, message = await setup()
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
async def test_wzgram_dispatches_each_warning_command_once(text, expected):
    router, store, _, _ = await setup()
    store.groups.get = AsyncMock(return_value=None)
    router.get_chat_member.return_value = SimpleNamespace(status="owner")
    group = raw.types.Chat(
        id=100,
        title="Test",
        photo=raw.types.ChatPhotoEmpty(),
        participants_count=2,
        date=int(NOW.timestamp()),
        version=1,
    )
    actor = raw.types.User(
        id=42, first_name="Actor", access_hash=42, usernames=[], restriction_reason=[]
    )
    target = User(id=55, is_bot=False, first_name="Target")
    router.message_cache[(-100, 9)] = Message(
        id=9, chat=Chat(id=-100, type=ChatType.GROUP), from_user=target
    )
    message = raw.types.Message(
        id=10,
        peer_id=raw.types.PeerChat(chat_id=100),
        from_id=raw.types.PeerUser(user_id=42),
        date=int(NOW.timestamp()),
        message=text,
        entities=[],
        restriction_reason=[],
        reply_to=raw.types.MessageReplyHeader(reply_to_msg_id=9),
    )
    update = raw.types.UpdateNewMessage(message=message, pts=1, pts_count=1)
    sent = asyncio.Event()

    async def deliver(*_args, **_kwargs):
        sent.set()

    with patch(
        "summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock, side_effect=deliver
    ) as private:
        await router.dispatcher.start()
        try:
            await router.dispatcher.updates_queue.put((update, {42: actor}, {100: group}))
            await asyncio.wait_for(sent.wait(), 2)
        finally:
            await router.dispatcher.stop()
    private.assert_awaited_once()
    assert store.groups.list_warnings.await_count == (1 if expected == "list" else 0)
    assert store.groups.revoke_warning.await_count == (1 if expected == "revoke" else 0)


async def test_warning_review_uses_summon_alert_without_making_content_public():
    from summon.bot.presentation import Emoji

    router, _, _, message = await setup()
    with (
        patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as public,
    ):
        await handler(router, "review_warnings")(message, SimpleNamespace(args=None))
    assert private.await_args.args[3].emoji is Emoji.ALERT
    public.assert_not_awaited()
