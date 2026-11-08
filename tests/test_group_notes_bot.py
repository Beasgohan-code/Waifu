"""Note commands are group-scoped, authorized, rate-limited and plain text."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pymongo.errors import AutoReconnect
from wzgram import Client, raw
from wzgram.types import User

from summon.bot.handlers import register_handlers
from summon.config import Settings
from summon.group_notes import NoteConflict

NOW = datetime(2026, 11, 5, tzinfo=UTC)
REV = "a" * 32
NOTE = {
    "name": "hello",
    "content": "<b>safe & public</b>\nSecond line",
    "revision": REV,
    "created_at": NOW,
    "updated_at": NOW,
    "created_by": 42,
    "updated_by": 42,
}


async def setup():
    notes = SimpleNamespace(
        list=AsyncMock(return_value=[NOTE]),
        get=AsyncMock(return_value=NOTE),
        save=AsyncMock(return_value=NOTE),
        delete=AsyncMock(return_value={"name": "hello", "status": "deleted"}),
    )
    store = SimpleNamespace(
        collection=object(),
        catalog=object(),
        trades=object(),
        gifts=object(),
        groups=SimpleNamespace(db=object()),
        notes=notes,
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
            status="administrator", privileges=SimpleNamespace(can_change_info=True)
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
        command = {
            "list_notes": "notes",
            "show_note": "note",
            "note_version": "noteinfo",
            "save_note": "savenote",
            "edit_note": "editnote",
            "delete_note": "delnote",
        }.get(name, name)
        message.command = [command]
        argument = getattr(args, "args", None)
        message.text = "/" + command + (" " + argument if argument else "")
        return await callback(router, message)

    return invoke


async def test_members_can_list_names_and_show_escaped_public_text_only():
    router, store, bot, message = await setup()
    bot.get_chat_member.return_value = SimpleNamespace(status="member")
    with (
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as public,
        patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private,
    ):
        await handler(router, "list_notes")(message)
        await handler(router, "show_note")(message, SimpleNamespace(args="HELLO"))
    assert bot.get_chat_member.await_args_list[0].args == (-100, 42)
    assert bot.get_chat_member.await_count == 2
    store.notes.list.assert_awaited_once_with(-100)
    store.notes.get.assert_awaited_once_with(-100, "hello")
    assert public.await_count == 2
    private.assert_not_awaited()
    listing = public.await_args_list[0].args[2].telegram_html(premium=False)
    text = public.await_args_list[1].args[2].telegram_html(premium=False)
    assert "hello" in listing and REV not in listing
    assert "&lt;b&gt;safe &amp; public&lt;/b&gt;" in text and "<b>safe" not in text
    assert REV not in text and "Second line" in text
    assert any(
        call.args == ("group:-100:notes_public",) for call in store.limiter.allow.await_args_list
    )


async def test_admin_create_only_and_edit_use_current_revision_and_private_result():
    router, store, bot, message = await setup()
    with (
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as public,
        patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private,
    ):
        await handler(router, "save_note")(message, SimpleNamespace(args="HELLO New text"))
        await handler(router, "edit_note")(message, SimpleNamespace(args=f"HELLO {REV} Newer text"))
    assert bot.get_chat_member.await_count == 2
    assert store.notes.save.await_args_list[0].args[:5] == (-100, 42, "hello", "New text", None)
    assert store.notes.save.await_args_list[1].args[:5] == (-100, 42, "hello", "Newer text", REV)
    assert isinstance(store.notes.save.await_args_list[0].args[5], datetime)
    assert private.await_count == 2
    public.assert_not_awaited()


async def test_note_version_is_admin_only_and_delivers_revision_privately():
    router, store, bot, message = await setup()
    with (
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as public,
        patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private,
    ):
        await handler(router, "note_version")(message, SimpleNamespace(args="hello"))
        bot.get_chat_member.return_value = SimpleNamespace(
            status="administrator", privileges=SimpleNamespace(can_change_info=False)
        )
        await handler(router, "note_version")(message, SimpleNamespace(args="hello"))
    assert store.notes.get.await_count == 1
    assert REV in private.await_args_list[0].args[3].telegram_html(premium=False)
    assert private.await_count == 2
    public.assert_not_awaited()


@pytest.mark.parametrize(
    "args",
    [
        None,
        "hello",
        f"hello {REV}",
        f"hello {REV} yes",
        f"hello {'X' * 32} confirm",
        f"hello {REV} confirm extra",
    ],
)
async def test_delete_requires_exact_revision_and_literal_confirmation(args):
    router, store, bot, message = await setup()
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock):
        await handler(router, "delete_note")(message, SimpleNamespace(args=args))
    store.notes.delete.assert_not_awaited()
    bot.get_chat_member.assert_not_awaited()


async def test_delete_uses_live_permission_current_revision_and_private_confirmation():
    router, store, bot, message = await setup()
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private:
        await handler(router, "delete_note")(message, SimpleNamespace(args=f"HELLO {REV} confirm"))
        bot.get_chat_member.return_value = SimpleNamespace(status="member")
        await handler(router, "delete_note")(message, SimpleNamespace(args=f"hello {REV} confirm"))
    assert store.notes.delete.await_count == 1
    assert store.notes.delete.await_args.args[:4] == (-100, 42, "hello", REV)
    assert private.await_count == 2


async def test_public_read_rejects_left_member_and_unauthorized_admin_changes():
    router, store, bot, message = await setup()
    with (
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as public,
        patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock),
    ):
        bot.get_chat_member.return_value = SimpleNamespace(status="left")
        await handler(router, "show_note")(message, SimpleNamespace(args="hello"))
        bot.get_chat_member.return_value = SimpleNamespace(status="member")
        await handler(router, "save_note")(message, SimpleNamespace(args="hello New"))
    store.notes.get.assert_not_awaited()
    store.notes.save.assert_not_awaited()
    public.assert_not_awaited()


async def test_rate_limit_and_invalid_or_anonymous_input_do_not_hit_storage():
    router, store, bot, message = await setup()
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock):
        store.limiter.allow.return_value = False
        await handler(router, "show_note")(message, SimpleNamespace(args="hello"))
        store.limiter.allow.return_value = True
        await handler(router, "save_note")(message, SimpleNamespace(args="bad.name text"))
        message.sender_chat = SimpleNamespace(id=-200)
        await handler(router, "save_note")(message, SimpleNamespace(args="hello text"))
    store.notes.get.assert_not_awaited()
    store.notes.save.assert_not_awaited()
    bot.get_chat_member.assert_not_awaited()


async def test_uncertainty_conflict_and_lookup_failures_are_sanitized():
    router, store, bot, message = await setup()
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private:
        store.notes.save.side_effect = AutoReconnect("secret db details")
        await handler(router, "save_note")(message, SimpleNamespace(args="hello text"))
        text = private.await_args.args[3].telegram_html(premium=False)
        assert "review" in text.lower() and "secret" not in text
        store.notes.save.side_effect = NoteConflict("Changed; reload notes")
        await handler(router, "save_note")(message, SimpleNamespace(args="hello text"))
        assert "reload" in private.await_args.args[3].telegram_html(premium=False).lower()
        bot.get_chat_member.side_effect = ConnectionError("secret network details")
        await handler(router, "note_version")(message, SimpleNamespace(args="hello"))
        assert "secret" not in private.await_args.args[3].telegram_html(premium=False)
    assert store.notes.save.await_count == 2


@pytest.mark.parametrize(
    "text,method",
    [
        ("/notes", "list"),
        ("/note hello", "get"),
        ("/noteinfo hello", "get"),
        ("/savenote hello New", "save"),
        (f"/editnote hello {REV} New", "save"),
        (f"/delnote hello {REV} confirm", "delete"),
    ],
)
async def test_wzgram_dispatches_each_note_command_once(text, method):
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
    message = raw.types.Message(
        id=10,
        peer_id=raw.types.PeerChat(chat_id=100),
        from_id=raw.types.PeerUser(user_id=42),
        date=int(NOW.timestamp()),
        message=text,
        entities=[],
        restriction_reason=[],
    )
    update = raw.types.UpdateNewMessage(message=message, pts=1, pts_count=1)
    sent = asyncio.Event()

    async def deliver(*_args, **_kwargs):
        sent.set()

    with (
        patch(
            "summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock, side_effect=deliver
        ),
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock, side_effect=deliver),
    ):
        await router.dispatcher.start()
        try:
            await router.dispatcher.updates_queue.put((update, {42: actor}, {100: group}))
            await asyncio.wait_for(sent.wait(), 2)
        finally:
            await router.dispatcher.stop()
    for name in ("list", "get", "save", "delete"):
        assert getattr(store.notes, name).await_count == (1 if name == method else 0)


async def test_note_menu_commands_give_private_chat_guidance_without_querying_other_groups():
    router, store, bot, message = await setup()
    message.chat = SimpleNamespace(id=42, type="private", title=None)
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as response:
        await handler(router, "list_notes")(message)
        await handler(router, "show_note")(message, SimpleNamespace(args="hello"))
    assert response.await_count == 2
    assert all(
        "group" in call.args[2].telegram_html(premium=False).lower()
        for call in response.await_args_list
    )
    bot.get_chat_member.assert_not_awaited()
    store.notes.list.assert_not_awaited()
    store.notes.get.assert_not_awaited()


async def test_public_notes_use_standard_escaped_text_even_when_rich_output_is_enabled():
    router, store, bot, message = await setup()
    bot.get_chat_member.return_value = SimpleNamespace(status="member")
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as public:
        await handler(router, "show_note")(message, SimpleNamespace(args="hello"))
    assert public.await_args.kwargs["rich"] is False
    assert "&lt;b&gt;safe &amp; public&lt;/b&gt;\nSecond line" in public.await_args.args[
        2
    ].telegram_html(premium=False)


async def test_notes_and_guidance_use_ios_icons_without_changing_privacy():
    from summon.bot.presentation import Emoji

    router, _, _, message = await setup()
    with (
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as public,
        patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private,
    ):
        await handler(router, "list_notes")(message)
        await handler(router, "show_note")(message, SimpleNamespace(args="hello"))
        await handler(router, "note_version")(message, SimpleNamespace(args="hello"))
        await handler(router, "save_note")(message, SimpleNamespace(args=None))
        await handler(router, "edit_note")(message, SimpleNamespace(args=None))
        await handler(router, "delete_note")(message, SimpleNamespace(args=None))
    assert [call.args[2].emoji for call in public.await_args_list] == [Emoji.NOTE, Emoji.NOTE]
    assert [call.args[3].emoji for call in private.await_args_list] == [
        Emoji.NOTE,
        Emoji.NOTE,
        Emoji.NOTE_EDIT,
        Emoji.NOTE_DELETE,
    ]
