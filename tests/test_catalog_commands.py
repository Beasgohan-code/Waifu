"""Real offline Wzgram dispatch for operator-only catalog commands."""

import asyncio
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image
from pymongo.errors import AutoReconnect
from wzgram import Client, raw
from wzgram.enums import ChatType
from wzgram.handlers import RawUpdateHandler
from wzgram.types import Chat, Message, Photo, User

from summon.bot.commands_catalog import _event_id
from summon.bot.handlers import register_handlers
from summon.config import Settings
from summon.services.artwork import MAX_ARTWORK_BYTES


def jpeg():
    out = BytesIO()
    Image.new("RGB", (16, 16), "purple").save(out, format="JPEG")
    return out.getvalue()


async def setup(*, operators=(42,), **settings_overrides):
    store = SimpleNamespace(
        groups=SimpleNamespace(db=object(), get=AsyncMock(return_value=None)),
        collection=object(),
        catalog=SimpleNamespace(
            create_character=AsyncMock(),
            update_character=AsyncMock(),
            catalog_event_exists=AsyncMock(return_value=False),
            character_exists=AsyncMock(return_value=False),
        ),
        trades=object(),
        gifts=object(),
        limiter=SimpleNamespace(
            allow=AsyncMock(return_value=True), claim_once=AsyncMock(return_value=True)
        ),
    )
    app = Client(
        "offline-catalog",
        api_id=123,
        api_hash="test",
        bot_token="123:fake",
        in_memory=True,
        workers=1,
        skip_updates=True,
        fetch_replies=False,
    )
    app.me = User(id=123, is_bot=True, username="summon_test", first_name="Bot")
    settings = {
        "operator_ids": list(operators),
        "rich_enabled": False,
        "premium_emoji_enabled": False,
        "_env_file": None,
    }
    settings.update(settings_overrides)
    register_handlers(app, store, Settings(**settings))
    app._test_complete = asyncio.Queue()

    async def finished(_client, update, _users, _chats):
        if isinstance(update, raw.types.UpdateUserStatus):
            app._test_complete.put_nowait(None)

    app.add_handler(RawUpdateHandler(finished), group=100)
    await asyncio.sleep(0)
    await app.dispatcher.start()
    return app, store


def packet(
    text,
    *,
    user_id=42,
    message_id=50,
    group=False,
    photo=None,
    reply_id=None,
    protected=False,
    spoiler=False,
    ttl=None,
):
    now = int(datetime.now(UTC).timestamp())
    user = raw.types.User(
        id=user_id, first_name="Operator", access_hash=user_id, usernames=[], restriction_reason=[]
    )
    users = {user_id: user}
    peer = raw.types.PeerUser(user_id=user_id)
    chats = {}
    if group:
        peer = raw.types.PeerChat(chat_id=100)
        chats[100] = raw.types.Chat(
            id=100,
            title="Test",
            photo=raw.types.ChatPhotoEmpty(),
            participants_count=2,
            date=now,
            version=1,
        )
    media = None
    if photo is not None:
        data, declared_size = photo
        media = raw.types.MessageMediaPhoto(
            spoiler=spoiler,
            ttl_seconds=ttl,
            photo=raw.types.Photo(
                id=900,
                access_hash=901,
                file_reference=b"ref",
                date=now,
                dc_id=1,
                sizes=[raw.types.PhotoSize(type="x", w=16, h=16, size=declared_size)],
            ),
        )
    reply = raw.types.MessageReplyHeader(reply_to_msg_id=reply_id) if reply_id else None
    message = raw.types.Message(
        id=message_id,
        peer_id=peer,
        from_id=raw.types.PeerUser(user_id=user_id),
        date=now,
        message=text,
        entities=[],
        restriction_reason=[],
        media=media,
        noforwards=protected,
        reply_to=reply,
    )
    return raw.types.UpdateNewMessage(message=message, pts=1, pts_count=1), users, chats


async def dispatch(app, update):
    await app.dispatcher.updates_queue.put(update)
    marker = raw.types.UpdateUserStatus(user_id=42, status=raw.types.UserStatusRecently())
    await app.dispatcher.updates_queue.put((marker, {}, {}))
    await asyncio.wait_for(app._test_complete.get(), 3)


async def test_https_upload_is_operator_only_create_and_actor_audited():
    app, store = await setup()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet("/upload aiko | Aiko | Sky | 3 | https://cdn.example/a.jpg"))
        store.catalog.create_character.assert_awaited_once()
        args = store.catalog.create_character.await_args
        character = args.args[0]
        assert (character.id, character.name, character.rarity) == (
            "aiko",
            "Aiko",
            "special-edition",
        )
        assert character.image_url == "https://cdn.example/a.jpg"
        assert args.kwargs["actor_id"] == 42
        assert args.kwargs["event_id"] == _event_id(123, 42, 50)
        assert args.kwargs["artwork"] is None
        assert sent.await_args.args[2].title == "Character added"
    finally:
        await app.dispatcher.stop()


async def test_addchar_reference_style_uses_deterministic_generated_id():
    app, store = await setup()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock):
            await dispatch(
                app, packet("/addchar Aiko_Tsuki Sky_Stories 4 https://cdn.example/a.jpg")
            )
            await dispatch(
                app, packet("/addchar Aiko_Tsuki Sky_Stories 4 https://cdn.example/a.jpg")
            )
        first, second = store.catalog.create_character.await_args_list
        assert first.args[0].id == second.args[0].id
        assert first.args[0].id.startswith("aiko-tsuki-")
        assert first.args[0].category == "Sky Stories"
        assert first.args[0].rarity == "legendary"
        assert first.kwargs["event_id"] == second.kwargs["event_id"]
    finally:
        await app.dispatcher.stop()


async def test_captioned_telegram_photo_is_sanitized_and_sent_to_transaction():
    app, store = await setup()
    original = jpeg()
    streamed = []

    async def stream(message, *, limit):
        streamed.append((message, limit))
        yield original

    app.stream_media = stream
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock):
            await dispatch(
                app, packet("/upload aiko | Aiko | Sky | 2", photo=(original, len(original)))
            )
        args = store.catalog.create_character.await_args
        assert args.args[0].image_url == args.kwargs["artwork"].path
        assert args.kwargs["artwork"].content.startswith(b"\xff\xd8\xff")
        assert streamed[0][1] == 4
    finally:
        await app.dispatcher.stop()


async def test_reply_to_photo_updates_image_without_touching_other_metadata():
    app, store = await setup()
    original = jpeg()
    photo = Photo(
        file_id="test",
        file_unique_id="test",
        width=16,
        height=16,
        file_size=len(original),
        date=datetime.now(UTC),
    )
    app.message_cache[(42, 49)] = Message(
        id=49, chat=Chat(id=42, type=ChatType.PRIVATE), photo=photo, from_user=User(id=42)
    )

    async def stream(message, *, limit):
        assert message.id == 49 and limit == 4
        yield original

    app.stream_media = stream
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock):
            await dispatch(app, packet("/updatechar aiko image", reply_id=49))
        args = store.catalog.update_character.await_args
        assert args.args[0] == "aiko"
        assert args.args[1].model_dump(exclude_none=True) == {
            "image_url": args.kwargs["artwork"].path
        }
        assert args.kwargs["actor_id"] == 42
    finally:
        await app.dispatcher.stop()


@pytest.mark.parametrize(
    "photo_options",
    [
        {"protected": True},
        {"spoiler": True},
        {"ttl": 10},
        {"photo": (b"x", MAX_ARTWORK_BYTES + 1)},
    ],
)
async def test_protected_spoiler_ttl_and_oversized_photos_are_never_downloaded(photo_options):
    app, store = await setup()
    original = jpeg()
    options = {"photo": (original, len(original)), **photo_options}
    app.stream_media = AsyncMock()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock):
            await dispatch(app, packet("/upload aiko | Aiko | Sky | 2", **options))
        app.stream_media.assert_not_called()
        store.catalog.create_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


@pytest.mark.parametrize(
    "text",
    [
        "/upload ../bad | Aiko | Sky | 2",
        "/upload aiko |    | Sky | 2",
        "/upload aiko | Aiko |    | 2",
        f"/upload aiko | {'x' * 121} | Sky | 2",
    ],
)
async def test_invalid_metadata_is_rejected_before_downloading_a_photo(text):
    app, store = await setup()
    original = jpeg()
    app.stream_media = AsyncMock()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet(text, photo=(original, len(original))))
        assert sent.await_args.args[2].title == "Check the character details"
        app.stream_media.assert_not_called()
        store.catalog.create_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_mismatched_download_size_never_writes_catalog():
    app, store = await setup()
    original = jpeg()

    async def stream(_message, *, limit):
        yield original[:-1]

    app.stream_media = stream
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock):
            await dispatch(
                app, packet("/upload aiko | Aiko | Sky | 2", photo=(original, len(original)))
            )
        store.catalog.create_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_non_operator_and_group_upload_never_fetch_or_write():
    app, store = await setup()
    app.stream_media = AsyncMock()
    original = jpeg()
    try:
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock),
            patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock),
        ):
            await dispatch(
                app, packet("/upload a | A | S | 1", user_id=99, photo=(original, len(original)))
            )
            await dispatch(
                app, packet("/upload a | A | S | 1", group=True, photo=(original, len(original)))
            )
        store.catalog.create_character.assert_not_awaited()
        app.stream_media.assert_not_called()
        store.limiter.allow.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


@pytest.mark.parametrize(
    "text",
    [
        "/upload a | A | S | 19 | https://cdn.example/a.jpg",
        "/upload a | A | S | 1 | https://127.0.0.1/a.jpg",
        "/upload ../../x | A | S | 1 | https://cdn.example/a.jpg",
        "/updatechar a owner 123",
        "/updatechar a rarity 20",
    ],
)
async def test_invalid_details_and_ownership_fields_are_rejected(text):
    app, store = await setup()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock):
            await dispatch(app, packet(text))
        store.catalog.create_character.assert_not_awaited()
        store.catalog.update_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_catalog_failures_are_sanitized_and_no_unsafe_retry_occurs(caplog):
    app, store = await setup()
    store.catalog.create_character.side_effect = AutoReconnect("FAKE_SECRET_DB_DETAIL")
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet("/upload a | A | S | 1 | https://cdn.example/a.jpg"))
        store.catalog.create_character.assert_awaited_once()
        assert "FAKE_SECRET_DB_DETAIL" not in caplog.text
        assert "FAKE_SECRET_DB_DETAIL" not in sent.await_args.args[2].telegram_html(premium=False)
        assert sent.await_args.args[2].title == "Result unconfirmed"
    finally:
        await app.dispatcher.stop()


async def test_imgbb_photo_upload_is_opt_in_claimed_once_and_used_as_catalog_url():
    app, store = await setup(artwork_backend="imgbb", imgbb_key="FAKE_HOST_KEY")
    original = jpeg()

    async def stream(_message, *, limit):
        yield original

    app.stream_media = stream
    try:
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.commands_catalog.upload_imgbb",
                new_callable=AsyncMock,
                return_value="https://i.ibb.co/abc/image.jpg",
            ) as hosted,
        ):
            await dispatch(app, packet("/upload a | A | S | 2", photo=(original, len(original))))
        store.catalog.character_exists.assert_awaited_once_with("a")
        store.catalog.catalog_event_exists.assert_awaited_once_with(_event_id(123, 42, 50))
        store.limiter.claim_once.assert_awaited_once_with(
            f"imgbb:{_event_id(123, 42, 50)}", seconds=7 * 86400
        )
        hosted.assert_awaited_once()
        character = store.catalog.create_character.await_args.args[0]
        assert character.image_url == "https://i.ibb.co/abc/image.jpg"
        assert store.catalog.create_character.await_args.kwargs["artwork"] is None
        assert sent.await_args.args[2].title == "Character added"
    finally:
        await app.dispatcher.stop()


@pytest.mark.parametrize("blocked_by", ["character", "audit", "claim"])
async def test_imgbb_duplicate_or_replayed_upload_never_contacts_host(blocked_by):
    app, store = await setup(artwork_backend="imgbb", imgbb_key="FAKE_HOST_KEY")
    original = jpeg()

    async def stream(_message, *, limit):
        yield original

    app.stream_media = stream
    if blocked_by == "character":
        store.catalog.character_exists.return_value = True
    elif blocked_by == "audit":
        store.catalog.catalog_event_exists.return_value = True
    else:
        store.limiter.claim_once.return_value = False
    try:
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch("summon.bot.commands_catalog.upload_imgbb", new_callable=AsyncMock) as hosted,
        ):
            await dispatch(app, packet("/upload a | A | S | 2", photo=(original, len(original))))
        hosted.assert_not_awaited()
        store.catalog.create_character.assert_not_awaited()
        assert sent.await_args.args[2].title in {"Already exists", "Upload already attempted"}
        if blocked_by == "character":
            store.catalog.catalog_event_exists.assert_not_awaited()
            store.limiter.claim_once.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_imgbb_timeout_is_unconfirmed_and_does_not_modify_catalog(caplog):
    from summon.services.imgbb import ImageHostError

    app, store = await setup(artwork_backend="imgbb", imgbb_key="FAKE_HOST_KEY")
    original = jpeg()

    async def stream(_message, *, limit):
        yield original

    app.stream_media = stream
    try:
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.commands_catalog.upload_imgbb",
                new_callable=AsyncMock,
                side_effect=ImageHostError("PRIVATE_HOST_DETAIL"),
            ) as hosted,
        ):
            await dispatch(app, packet("/upload a | A | S | 2", photo=(original, len(original))))
        hosted.assert_awaited_once()
        store.catalog.create_character.assert_not_awaited()
        card = sent.await_args.args[2]
        assert card.title == "Image host unconfirmed" and "catalog was not changed" in card.body
        assert "PRIVATE_HOST_DETAIL" not in caplog.text + card.body
    finally:
        await app.dispatcher.stop()


async def test_imgbb_success_then_database_failure_warns_about_possible_orphan():
    app, store = await setup(artwork_backend="imgbb", imgbb_key="FAKE_HOST_KEY")
    original = jpeg()

    async def stream(_message, *, limit):
        yield original

    app.stream_media = stream
    store.catalog.create_character.side_effect = AutoReconnect("PRIVATE_DATABASE_DETAIL")
    try:
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.commands_catalog.upload_imgbb",
                new_callable=AsyncMock,
                return_value="https://i.ibb.co/abc/image.jpg",
            ) as hosted,
        ):
            await dispatch(app, packet("/upload a | A | S | 2", photo=(original, len(original))))
        hosted.assert_awaited_once()
        store.catalog.create_character.assert_awaited_once()
        card = sent.await_args.args[2]
        assert card.title == "Result unconfirmed" and "ImgBB image may also exist" in card.body
    finally:
        await app.dispatcher.stop()


async def test_configured_channel_receives_sanitized_photo_only_after_catalog_save():
    app, store = await setup(db_channel=-1001234567890)
    original = jpeg()
    events = []

    async def stream(_message, *, limit):
        yield original

    async def save(*_args, **_kwargs):
        events.append("catalog saved")

    async def post(**kwargs):
        assert events == ["catalog saved"]
        assert kwargs["chat_id"] == -1001234567890
        assert kwargs["photo"].getvalue().startswith(b"\xff\xd8\xff")
        events.append("channel posted")
        return object()

    app.stream_media = stream
    app.get_chat = AsyncMock(return_value=SimpleNamespace(type=ChatType.CHANNEL))
    app.send_photo = AsyncMock(side_effect=post)
    store.catalog.create_character.side_effect = save
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet("/upload a | A | S | 2", photo=(original, len(original))))
        assert events == ["catalog saved", "channel posted"]
        assert sent.await_args.args[2].title == "Character added"
        assert "unconfirmed" not in sent.await_args.args[2].body
    finally:
        await app.dispatcher.stop()


async def test_db_channel_never_posts_to_a_group_and_warns_after_catalog_save():
    app, store = await setup(db_channel=-1001234567890)
    original = jpeg()

    async def stream(_message, *, limit):
        yield original

    app.stream_media = stream
    app.get_chat = AsyncMock(return_value=SimpleNamespace(type=ChatType.SUPERGROUP))
    app.send_photo = AsyncMock()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet("/upload a | A | S | 2", photo=(original, len(original))))
        store.catalog.create_character.assert_awaited_once()
        app.send_photo.assert_not_awaited()
        assert "Channel copy unconfirmed" in sent.await_args.args[2].body
    finally:
        await app.dispatcher.stop()


async def test_db_channel_timeout_does_not_retry_or_undo_confirmed_catalog():
    app, store = await setup(db_channel=-1001234567890)
    original = jpeg()

    async def stream(_message, *, limit):
        yield original

    app.stream_media = stream
    app.get_chat = AsyncMock(return_value=SimpleNamespace(type=ChatType.CHANNEL))
    app.send_photo = AsyncMock(side_effect=TimeoutError("PRIVATE_SEND_DETAIL"))
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet("/upload a | A | S | 2", photo=(original, len(original))))
        store.catalog.create_character.assert_awaited_once()
        app.send_photo.assert_awaited_once()
        assert "Channel copy unconfirmed" in sent.await_args.args[2].body
        assert "PRIVATE_SEND_DETAIL" not in sent.await_args.args[2].body
    finally:
        await app.dispatcher.stop()


async def test_db_channel_does_not_fetch_or_archive_operator_https_urls():
    app, store = await setup(db_channel=-1001234567890)
    app.get_chat = AsyncMock()
    app.send_photo = AsyncMock()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock):
            await dispatch(app, packet("/upload a | A | S | 2 | https://cdn.example/a.jpg"))
        store.catalog.create_character.assert_awaited_once()
        app.get_chat.assert_not_awaited()
        app.send_photo.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_imgbb_photo_update_requires_existing_id_and_never_changes_ownership():
    app, store = await setup(artwork_backend="imgbb", imgbb_key="FAKE_HOST_KEY")
    store.catalog.character_exists.return_value = True
    original = jpeg()

    async def stream(_message, *, limit):
        yield original

    app.stream_media = stream
    try:
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock),
            patch(
                "summon.bot.commands_catalog.upload_imgbb",
                new_callable=AsyncMock,
                return_value="https://i.ibb.co/abc/image.jpg",
            ),
        ):
            await dispatch(app, packet("/updatechar a image", photo=(original, len(original))))
        args = store.catalog.update_character.await_args
        assert args.args[0] == "a"
        assert args.args[1].model_dump(exclude_none=True) == {
            "image_url": "https://i.ibb.co/abc/image.jpg"
        }
        assert args.kwargs["artwork"] is None and args.kwargs["actor_id"] == 42
    finally:
        await app.dispatcher.stop()


async def test_imgbb_key_alone_does_not_upload_outside_mongo():
    app, store = await setup(imgbb_key="FAKE_HOST_KEY")
    original = jpeg()

    async def stream(_message, *, limit):
        yield original

    app.stream_media = stream
    try:
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock),
            patch("summon.bot.commands_catalog.upload_imgbb", new_callable=AsyncMock) as hosted,
        ):
            await dispatch(app, packet("/upload a | A | S | 2", photo=(original, len(original))))
        hosted.assert_not_awaited()
        store.limiter.claim_once.assert_not_awaited()
        data = store.catalog.create_character.await_args
        assert data.args[0].image_url == data.kwargs["artwork"].path
    finally:
        await app.dispatcher.stop()


async def test_imgbb_mode_still_rejects_protected_media_before_network():
    app, store = await setup(artwork_backend="imgbb", imgbb_key="FAKE_HOST_KEY")
    original = jpeg()
    app.stream_media = AsyncMock()
    try:
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock),
            patch("summon.bot.commands_catalog.upload_imgbb", new_callable=AsyncMock) as hosted,
        ):
            await dispatch(
                app,
                packet("/upload a | A | S | 2", photo=(original, len(original)), protected=True),
            )
        app.stream_media.assert_not_called()
        hosted.assert_not_awaited()
        store.limiter.claim_once.assert_not_awaited()
        store.catalog.create_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


@pytest.mark.parametrize("command", ["owner", "panel"])
async def test_operator_panel_opens_existing_mini_app_only_for_private_operator(command):
    app, store = await setup(webapp_url="https://miniapp.example", premium_emoji_enabled=True)
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet(f"/{command}"))
            card = sent.await_args.args[2]
            button = sent.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
        assert card.title == "Operator panel"
        assert "server-side operator checks" in card.body
        assert button.web_app.url == "https://miniapp.example"
        assert button.icon_custom_emoji_id is not None
        store.catalog.create_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_operator_list_is_read_only_private_and_never_leaked_in_group():
    app, store = await setup(operators=(42, 55))
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet("/sudolist"))
            card = sent.await_args.args[2]
        assert card.title == "Configured operators"
        assert card.fields == (("Operator 1", "42"), ("Operator 2", "55"))
        assert "cannot be changed in chat" in card.body
        store.catalog.create_character.assert_not_awaited()

        with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private:
            await dispatch(app, packet("/sudolist", group=True))
            group_card = private.await_args.args[3]
        assert group_card.title == "Use a private chat"
        assert group_card.fields == ()
    finally:
        await app.dispatcher.stop()


async def test_operator_panel_and_list_deny_non_operator():
    app, store = await setup()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet("/panel", user_id=7))
            await dispatch(app, packet("/sudolist", user_id=7, message_id=51))
        assert [call.args[2].title for call in sent.await_args_list] == [
            "Operator access required",
            "Operator access required",
        ]
        store.limiter.allow.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_operator_panel_without_url_explains_configuration_without_button():
    app, _store = await setup()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet("/owner"))
        assert "Set WEBAPP_URL" in sent.await_args.args[2].body
        assert sent.await_args.kwargs["reply_markup"] is None
    finally:
        await app.dispatcher.stop()


async def test_operator_list_caps_response_fields():
    app, _store = await setup(operators=tuple(range(40, 70)))
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet("/sudolist"))
        card = sent.await_args.args[2]
        assert len(card.fields) == 20
        assert "Showing 20 of 30" in card.body
    finally:
        await app.dispatcher.stop()
