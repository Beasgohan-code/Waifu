"""Note commands are group-scoped, authorized, rate-limited and plain text."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetChatMember
from pymongo.errors import AutoReconnect

from summon.bot.handlers import build_router
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


def setup():
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
    router = build_router(store, Settings(_env_file=None))
    bot = SimpleNamespace(
        id=99,
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status="administrator", can_change_info=True)
        ),
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(
            id=42, is_bot=False, first_name="Actor", last_name=None, username=None
        ),
        sender_chat=None,
        chat=SimpleNamespace(id=-100, type="supergroup", title="Test"),
        message_thread_id=None,
        ephemeral_message_id=None,
        bot=bot,
    )
    return router, store, bot, message


def handler(router, name):
    return next(item.callback for item in router.message.handlers if item.callback.__name__ == name)


async def test_members_can_list_names_and_show_escaped_public_text_only():
    router, store, bot, message = setup()
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
    router, store, bot, message = setup()
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
    router, store, bot, message = setup()
    with (
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as public,
        patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private,
    ):
        await handler(router, "note_version")(message, SimpleNamespace(args="hello"))
        bot.get_chat_member.return_value = SimpleNamespace(
            status="administrator", can_change_info=False
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
    router, store, bot, message = setup()
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock):
        await handler(router, "delete_note")(message, SimpleNamespace(args=args))
    store.notes.delete.assert_not_awaited()
    bot.get_chat_member.assert_not_awaited()


async def test_delete_uses_live_permission_current_revision_and_private_confirmation():
    router, store, bot, message = setup()
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private:
        await handler(router, "delete_note")(message, SimpleNamespace(args=f"HELLO {REV} confirm"))
        bot.get_chat_member.return_value = SimpleNamespace(status="member")
        await handler(router, "delete_note")(message, SimpleNamespace(args=f"hello {REV} confirm"))
    assert store.notes.delete.await_count == 1
    assert store.notes.delete.await_args.args[:4] == (-100, 42, "hello", REV)
    assert private.await_count == 2


async def test_public_read_rejects_left_member_and_unauthorized_admin_changes():
    router, store, bot, message = setup()
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
    router, store, bot, message = setup()
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
    router, store, bot, message = setup()
    with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private:
        store.notes.save.side_effect = AutoReconnect("secret db details")
        await handler(router, "save_note")(message, SimpleNamespace(args="hello text"))
        text = private.await_args.args[3].telegram_html(premium=False)
        assert "review" in text.lower() and "secret" not in text
        store.notes.save.side_effect = NoteConflict("Changed; reload notes")
        await handler(router, "save_note")(message, SimpleNamespace(args="hello text"))
        assert "reload" in private.await_args.args[3].telegram_html(premium=False).lower()
        bot.get_chat_member.side_effect = TelegramNetworkError(
            method=GetChatMember(chat_id=-100, user_id=42), message="secret network details"
        )
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
async def test_aiogram_dispatches_each_note_command_once(text, method):
    from aiogram import Bot, Dispatcher
    from aiogram.types import Update

    router, store, _, _ = setup()
    store.groups.get = AsyncMock(return_value=None)
    bot = Bot("123456:TEST_TOKEN_NOT_REAL")
    dp = Dispatcher()
    dp.include_router(router)
    command = text.split()[0]
    update = Update.model_validate(
        {
            "update_id": 123,
            "message": {
                "message_id": 10,
                "date": int(NOW.timestamp()),
                "chat": {"id": -100, "type": "supergroup", "title": "Test"},
                "from": {"id": 42, "is_bot": False, "first_name": "Actor"},
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(command)}],
            },
        },
        context={"bot": bot},
    )
    with (
        patch.object(
            Bot,
            "get_chat_member",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(status="creator"),
        ),
        patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock),
        patch("summon.bot.handlers.send_card", new_callable=AsyncMock),
    ):
        await dp.feed_update(bot, update)
    for name in ("list", "get", "save", "delete"):
        assert getattr(store.notes, name).await_count == (1 if name == method else 0)
    await bot.session.close()


async def test_note_menu_commands_give_private_chat_guidance_without_querying_other_groups():
    router, store, bot, message = setup()
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
    _, store, bot, message = setup()
    bot.get_chat_member.return_value = SimpleNamespace(status="member")
    router = build_router(
        store, Settings(bot_api_rich_enabled=True, _env_file=None), rich_client=object()
    )
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as public:
        await handler(router, "show_note")(message, SimpleNamespace(args="hello"))
    assert public.await_args.kwargs["rich_client"] is None
    assert public.await_args.kwargs["bot_api_rich"] is False
    assert "&lt;b&gt;safe &amp; public&lt;/b&gt;\nSecond line" in public.await_args.args[
        2
    ].telegram_html(premium=False)


async def test_notes_and_guidance_use_ios_icons_without_changing_privacy():
    from summon.bot.presentation import Emoji

    router, _, _, message = setup()
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
