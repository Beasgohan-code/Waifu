"""Native dispatcher protection precedes commands, spawns and edited messages."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from wzgram import Client, raw
from wzgram.handlers import RawUpdateHandler
from wzgram.types import Message, User

from summon.bot.handlers import register_handlers
from summon.config import Settings
from summon.models import GroupSettings


@pytest_asyncio.fixture
async def make_bot():
    clients = []

    async def create(*, database=None, **settings):
        groups = SimpleNamespace(
            db=database if database is not None else object(),
            get=AsyncMock(return_value={"settings": GroupSettings(**settings).model_dump()}),
            record_spam=AsyncMock(),
            ensure=AsyncMock(),
        )
        store = SimpleNamespace(
            groups=groups,
            collection=SimpleNamespace(
                record_message=AsyncMock(return_value={"settings": {"spawn_enabled": False}})
            ),
            catalog=object(),
            trades=object(),
            gifts=object(),
            limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
        )
        app = Client(
            "offline-protection",
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
            app, store, Settings(rich_enabled=False, premium_emoji_enabled=False, _env_file=None)
        )
        app._test_complete = asyncio.Queue()

        async def finished(_client, update, _users, _chats):
            if isinstance(update, raw.types.UpdateUserStatus):
                app._test_complete.put_nowait(None)

        app.add_handler(RawUpdateHandler(finished), group=100)
        await asyncio.sleep(0)
        await app.dispatcher.start()
        clients.append(app)
        return app, store

    yield create
    for client in clients:
        await client.dispatcher.stop()


def packet(
    text="",
    *,
    edited=False,
    private=False,
    ephemeral=False,
    photo=False,
    hidden_link=False,
    service=None,
    sender_chat=False,
):
    now = int(datetime.now(UTC).timestamp())
    actor = raw.types.User(
        id=42, first_name="Member", access_hash=42, usernames=[], restriction_reason=[]
    )
    new = raw.types.User(
        id=43, first_name="New", access_hash=43, usernames=[], restriction_reason=[]
    )
    group = raw.types.Chat(
        id=100,
        title="Test",
        photo=raw.types.ChatPhotoEmpty(),
        participants_count=2,
        date=now,
        version=1,
    )
    peer = raw.types.PeerUser(user_id=42) if private else raw.types.PeerChat(chat_id=100)
    sender = raw.types.PeerUser(user_id=42)
    chats = {100: group}
    if sender_chat:
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
    users = {42: actor, 43: new}
    if ephemeral:
        message = raw.types.EphemeralMessage(
            id=999,
            from_id=sender,
            receiver_id=123,
            date=now,
            message=text,
            peer_id=peer,
            entities=[],
        )
        return raw.types.UpdateNewEphemeralMessage(message=message), users, chats
    if service:
        action = (
            raw.types.MessageActionChatAddUser(users=[43])
            if service == "join"
            else raw.types.MessageActionPinMessage()
        )
        message = raw.types.MessageService(
            id=123, peer_id=peer, from_id=sender, date=now, action=action
        )
    else:
        media = None
        if photo:
            media = raw.types.MessageMediaPhoto(
                photo=raw.types.Photo(
                    id=1,
                    access_hash=2,
                    file_reference=b"ref",
                    date=now,
                    dc_id=1,
                    sizes=[raw.types.PhotoSize(type="x", w=1, h=1, size=1)],
                )
            )
        entities = [raw.types.MessageEntityTextUrl(offset=0, length=5, url="https://example.com")]
        message = raw.types.Message(
            id=123,
            peer_id=peer,
            from_id=sender,
            date=now,
            message=text,
            entities=entities if hidden_link else [],
            restriction_reason=[],
            media=media,
        )
    kind = raw.types.UpdateEditMessage if edited else raw.types.UpdateNewMessage
    return kind(message=message, pts=1, pts_count=1), users, chats


async def dispatch(app, update):
    await app.dispatcher.updates_queue.put(update)
    marker = raw.types.UpdateUserStatus(user_id=42, status=raw.types.UserStatusRecently())
    await app.dispatcher.updates_queue.put((marker, {}, {}))
    await asyncio.wait_for(app._test_complete.get(), 2)


def bot_admin(*, can_delete=True):
    return SimpleNamespace(
        status="administrator", privileges=SimpleNamespace(can_delete_messages=can_delete)
    )


async def test_protection_blocks_command_before_handler_and_audits_confirmed_deletion(make_bot):
    app, store = await make_bot(link_filter=True)
    app.get_chat_member = AsyncMock(side_effect=[SimpleNamespace(status="member"), bot_admin()])
    with (
        patch.object(Message, "delete", new_callable=AsyncMock, return_value=True) as delete,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
    ):
        await dispatch(app, packet("/help https://example.com"))
    delete.assert_awaited_once()
    assert store.groups.record_spam.await_args.args[:3] == (-100, 42, "link")
    send.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()


async def test_administrators_are_exempt_and_command_still_runs(make_bot):
    app, store = await make_bot(link_filter=True)
    app.get_chat_member = AsyncMock(return_value=bot_admin())
    with (
        patch.object(Message, "delete", new_callable=AsyncMock) as delete,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
    ):
        await dispatch(app, packet("/help https://example.com"))
    delete.assert_not_awaited()
    store.groups.record_spam.assert_not_awaited()
    send.assert_awaited_once()


async def test_suspected_command_is_not_executed_if_bot_cannot_delete(make_bot):
    app, store = await make_bot(link_filter=True)
    app.get_chat_member = AsyncMock(
        side_effect=[SimpleNamespace(status="member"), bot_admin(can_delete=False)]
    )
    with (
        patch.object(Message, "delete", new_callable=AsyncMock) as delete,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
    ):
        await dispatch(app, packet("/help https://example.com"))
    delete.assert_not_awaited()
    store.groups.record_spam.assert_not_awaited()
    send.assert_not_awaited()


async def test_edited_messages_are_inspected_but_never_count_as_activity(make_bot):
    app, store = await make_bot(link_filter=True, flood_enabled=True)
    app.get_chat_member = AsyncMock(side_effect=[SimpleNamespace(status="member"), bot_admin()])
    with patch.object(Message, "delete", new_callable=AsyncMock, return_value=True) as delete:
        await dispatch(app, packet("https://example.com", edited=True))
    delete.assert_awaited_once()
    store.groups.record_spam.assert_awaited_once()
    store.collection.record_message.assert_not_awaited()


async def test_private_messages_do_not_use_group_protection(make_bot):
    app, store = await make_bot(link_filter=True)
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(app, packet("/help https://example.com", private=True))
    store.groups.get.assert_not_awaited()
    send.assert_awaited_once()


async def test_suspected_ephemeral_command_is_not_deleted_as_ordinary_message(make_bot):
    app, store = await make_bot(link_filter=True)
    app.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    with (
        patch.object(Message, "delete", new_callable=AsyncMock) as delete,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
    ):
        await dispatch(app, packet("/help https://example.com", ephemeral=True))
    app.get_chat_member.assert_awaited_once_with(-100, 42)
    delete.assert_not_awaited()
    store.groups.record_spam.assert_not_awaited()
    send.assert_not_awaited()


async def test_new_chat_activity_is_checked_and_counted_once(make_bot):
    counter = AsyncMock(return_value={"count": 1})
    database = SimpleNamespace(rate_limits=SimpleNamespace(find_one_and_update=counter))
    app, store = await make_bot(database=database, flood_enabled=True)
    app.get_chat_member = AsyncMock()
    await dispatch(app, packet("A normal conversation"))
    counter.assert_awaited_once()
    store.groups.ensure.assert_awaited_once()
    store.collection.record_message.assert_awaited_once()
    app.get_chat_member.assert_not_awaited()


async def test_safe_edit_does_not_count_flood_spawn_or_run_command(make_bot):
    counter = AsyncMock(return_value={"count": 1})
    database = SimpleNamespace(rate_limits=SimpleNamespace(find_one_and_update=counter))
    app, store = await make_bot(database=database, flood_enabled=True, duplicate_enabled=True)
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(app, packet("/help", edited=True))
    counter.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()
    store.groups.ensure.assert_not_awaited()
    send.assert_not_awaited()


async def test_join_service_event_bypasses_spam_and_still_welcomes(make_bot):
    app, store = await make_bot(welcome_enabled=True, link_filter=True, flood_enabled=True)
    app.get_chat_member = AsyncMock()
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send:
        await dispatch(app, packet(service="join"))
    store.groups.get.assert_awaited_once_with(-100)
    app.get_chat_member.assert_not_awaited()
    send.assert_awaited_once()
    store.collection.record_message.assert_not_awaited()


async def test_edited_caption_hidden_link_is_checked(make_bot):
    app, store = await make_bot(link_filter=True)
    app.get_chat_member = AsyncMock(side_effect=[SimpleNamespace(status="member"), bot_admin()])
    with patch.object(Message, "delete", new_callable=AsyncMock, return_value=True) as delete:
        await dispatch(app, packet("click here", edited=True, photo=True, hidden_link=True))
    delete.assert_awaited_once()
    assert store.groups.record_spam.await_args.args[:3] == (-100, 42, "link")
    store.collection.record_message.assert_not_awaited()


async def test_unverified_admin_status_suppresses_flagged_command_without_deletion(make_bot):
    app, store = await make_bot(link_filter=True)
    app.get_chat_member = AsyncMock(side_effect=ConnectionError("synthetic secret"))
    with (
        patch.object(Message, "delete", new_callable=AsyncMock) as delete,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
    ):
        await dispatch(app, packet("/help https://example.com"))
    delete.assert_not_awaited()
    store.groups.record_spam.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.parametrize("result", [False, ConnectionError("unconfirmed")])
async def test_unconfirmed_delete_does_not_create_false_audit_or_run_command(make_bot, result):
    app, store = await make_bot(link_filter=True)
    app.get_chat_member = AsyncMock(side_effect=[SimpleNamespace(status="member"), bot_admin()])
    kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
    with (
        patch.object(Message, "delete", new_callable=AsyncMock, **kwargs) as delete,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
    ):
        await dispatch(app, packet("/help https://example.com"))
    delete.assert_awaited_once()
    store.groups.record_spam.assert_not_awaited()
    send.assert_not_awaited()


async def test_flooding_a_command_is_checked_once_before_handler(make_bot):
    counter = AsyncMock(return_value={"count": 4})
    database = SimpleNamespace(rate_limits=SimpleNamespace(find_one_and_update=counter))
    app, store = await make_bot(database=database, flood_enabled=True, flood_limit=3)
    app.get_chat_member = AsyncMock(side_effect=[SimpleNamespace(status="member"), bot_admin()])
    with (
        patch.object(Message, "delete", new_callable=AsyncMock, return_value=True) as delete,
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as send,
    ):
        await dispatch(app, packet("/help"))
    counter.assert_awaited_once()
    delete.assert_awaited_once()
    assert store.groups.record_spam.await_args.args[:3] == (-100, 42, "flood")
    send.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()


async def test_other_group_service_events_do_not_count_as_flood_or_spawns(make_bot):
    app, store = await make_bot(flood_enabled=True)
    await dispatch(app, packet(service="pin"))
    store.groups.get.assert_not_awaited()
    store.groups.ensure.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()


async def test_sender_chat_is_not_deleted_without_checkable_human_role(make_bot):
    app, store = await make_bot(link_filter=True)
    with patch.object(Message, "delete", new_callable=AsyncMock) as delete:
        await dispatch(app, packet("https://example.com", sender_chat=True))
    store.groups.get.assert_not_awaited()
    store.groups.ensure.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()
    delete.assert_not_awaited()


async def test_flagged_ordinary_message_never_advances_spawn_activity(make_bot):
    app, store = await make_bot(link_filter=True)
    app.get_chat_member = AsyncMock(side_effect=[SimpleNamespace(status="member"), bot_admin()])
    with patch.object(Message, "delete", new_callable=AsyncMock, return_value=True) as delete:
        await dispatch(app, packet("https://example.com"))
    delete.assert_awaited_once()
    store.groups.record_spam.assert_awaited_once()
    store.groups.ensure.assert_not_awaited()
    store.collection.record_message.assert_not_awaited()
