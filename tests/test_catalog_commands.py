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

from summon.bot.handlers import register_handlers
from summon.bot.modules.catalog_admin import _event_id
from summon.config import Settings
from summon.rarities import RARITY_WEIGHTS
from summon.services.artwork import MAX_ARTWORK_BYTES


def jpeg():
    out = BytesIO()
    Image.new("RGB", (16, 16), "purple").save(out, format="JPEG")
    return out.getvalue()


async def setup(*, operators=(42,), **settings_overrides):
    store = SimpleNamespace(
        groups=SimpleNamespace(db=object(), get=AsyncMock(return_value=None)),
        collection=SimpleNamespace(
            claim_daily_character=AsyncMock(
                return_value={"id": "aiko", "name": "Aiko", "category": "Sky", "rarity": "rare"}
            )
        ),
        catalog=SimpleNamespace(
            choose=AsyncMock(return_value={"_id": "aiko"}),
            spawn_weights=AsyncMock(return_value=dict(RARITY_WEIGHTS)),
            create_character=AsyncMock(),
            update_character=AsyncMock(),
            replace_media_reviewed=AsyncMock(),
            edit_state=AsyncMock(),
            prepare_numbering=AsyncMock(),
            catalog_event_exists=AsyncMock(return_value=False),
            character_exists=AsyncMock(return_value=False),
            set_spawn_weight=AsyncMock(return_value=(10, 500)),
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
    app._test_settings = Settings(**settings)
    register_handlers(app, store, app._test_settings)
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


async def test_captioned_telegram_photo_offers_host_choice_before_uploading():
    app, store = await setup(db_channel=-1001234567890, imgbb_key="FAKE_HOST_KEY")
    original = jpeg()
    app.stream_media = AsyncMock()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(
                app, packet("/upload aiko | Aiko | Sky | 2", photo=(original, len(original)))
            )
        assert sent.await_args.args[2].title == "Choose a media host"
        buttons = sent.await_args.kwargs["reply_markup"].inline_keyboard[0]
        assert len(buttons) == 2
        assert "Catbox" in buttons[0].text and "ImgBB" in buttons[1].text
        app.stream_media.assert_not_called()
        store.catalog.create_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_reply_to_photo_updates_image_without_touching_other_metadata():
    app, store = await setup(artwork_backend="imgbb", imgbb_key="FAKE_HOST_KEY")
    store.catalog.character_exists.return_value = True
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
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock),
            patch(
                "summon.bot.modules.catalog_admin.upload_imgbb",
                new_callable=AsyncMock,
                return_value="https://i.ibb.co/test/photo.jpg",
            ),
        ):
            await dispatch(app, packet("/updatechar aiko image", reply_id=49))
        args = store.catalog.update_character.await_args
        assert args.args[0] == "aiko"
        assert args.args[1].model_dump(exclude_none=True) == {
            "image_url": "https://i.ibb.co/test/photo.jpg"
        }
        assert args.kwargs["artwork"] is None
        assert args.kwargs["telegram_file_id"] == "test"
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
                "summon.bot.modules.catalog_admin.upload_imgbb",
                new_callable=AsyncMock,
                return_value="https://i.ibb.co/abc/image.jpg",
            ) as hosted,
        ):
            await dispatch(app, packet("/addchar A | S | 2", photo=(original, len(original))))
        store.catalog.character_exists.assert_awaited_once_with("a-" + _event_id(123, 42, 50)[-12:])
        store.catalog.catalog_event_exists.assert_awaited_once_with(_event_id(123, 42, 50))
        store.limiter.claim_once.assert_awaited_once_with(
            f"imgbb:{_event_id(123, 42, 50)}", seconds=7 * 86400
        )
        hosted.assert_awaited_once()
        character = store.catalog.create_character.await_args.args[0]
        assert character.image_url == "https://i.ibb.co/abc/image.jpg"
        assert character.telegram_file_id
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
            patch(
                "summon.bot.modules.catalog_admin.upload_imgbb", new_callable=AsyncMock
            ) as hosted,
        ):
            await dispatch(app, packet("/addchar A | S | 2", photo=(original, len(original))))
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
                "summon.bot.modules.catalog_admin.upload_imgbb",
                new_callable=AsyncMock,
                side_effect=ImageHostError("PRIVATE_HOST_DETAIL"),
            ) as hosted,
        ):
            await dispatch(app, packet("/addchar A | S | 2", photo=(original, len(original))))
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
                "summon.bot.modules.catalog_admin.upload_imgbb",
                new_callable=AsyncMock,
                return_value="https://i.ibb.co/abc/image.jpg",
            ) as hosted,
        ):
            await dispatch(app, packet("/addchar A | S | 2", photo=(original, len(original))))
        hosted.assert_awaited_once()
        store.catalog.create_character.assert_awaited_once()
        card = sent.await_args.args[2]
        assert card.title == "Result unconfirmed" and "ImgBB image may also exist" in card.body
    finally:
        await app.dispatcher.stop()


async def test_configured_channel_receives_sanitized_photo_only_after_catalog_save():
    app, store = await setup(
        db_channel=-1001234567890, artwork_backend="imgbb", imgbb_key="FAKE_HOST_KEY"
    )
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
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.modules.catalog_admin.upload_imgbb",
                new_callable=AsyncMock,
                return_value="https://i.ibb.co/test/photo.jpg",
            ),
        ):
            await dispatch(app, packet("/addchar A | S | 2", photo=(original, len(original))))
        assert events == ["catalog saved", "channel posted"]
        assert sent.await_args.args[2].title == "Character added"
        assert "unconfirmed" not in sent.await_args.args[2].body
    finally:
        await app.dispatcher.stop()


async def test_db_channel_never_posts_to_a_group_and_warns_after_catalog_save():
    app, store = await setup(
        db_channel=-1001234567890, artwork_backend="imgbb", imgbb_key="FAKE_HOST_KEY"
    )
    original = jpeg()

    async def stream(_message, *, limit):
        yield original

    app.stream_media = stream
    app.get_chat = AsyncMock(return_value=SimpleNamespace(type=ChatType.SUPERGROUP))
    app.send_photo = AsyncMock()
    try:
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.modules.catalog_admin.upload_imgbb",
                new_callable=AsyncMock,
                return_value="https://i.ibb.co/test/photo.jpg",
            ),
        ):
            await dispatch(app, packet("/addchar A | S | 2", photo=(original, len(original))))
        store.catalog.create_character.assert_awaited_once()
        app.send_photo.assert_not_awaited()
        assert "Channel copy unconfirmed" in sent.await_args.args[2].body
    finally:
        await app.dispatcher.stop()


async def test_db_channel_timeout_does_not_retry_or_undo_confirmed_catalog():
    app, store = await setup(
        db_channel=-1001234567890, artwork_backend="imgbb", imgbb_key="FAKE_HOST_KEY"
    )
    original = jpeg()

    async def stream(_message, *, limit):
        yield original

    app.stream_media = stream
    app.get_chat = AsyncMock(return_value=SimpleNamespace(type=ChatType.CHANNEL))
    app.send_photo = AsyncMock(side_effect=TimeoutError("PRIVATE_SEND_DETAIL"))
    try:
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.modules.catalog_admin.upload_imgbb",
                new_callable=AsyncMock,
                return_value="https://i.ibb.co/test/photo.jpg",
            ),
        ):
            await dispatch(app, packet("/addchar A | S | 2", photo=(original, len(original))))
        store.catalog.create_character.assert_awaited_once()
        app.send_photo.assert_awaited_once()
        assert "Channel copy unconfirmed" in sent.await_args.args[2].body
        assert "PRIVATE_SEND_DETAIL" not in sent.await_args.args[2].body
    finally:
        await app.dispatcher.stop()


async def test_db_channel_does_not_fetch_or_archive_operator_https_urls():
    app, store = await setup(
        db_channel=-1001234567890, artwork_backend="imgbb", imgbb_key="FAKE_HOST_KEY"
    )
    app.get_chat = AsyncMock()
    app.send_photo = AsyncMock()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock):
            await dispatch(app, packet("/addchar A | S | 2 | https://cdn.example/a.jpg"))
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
                "summon.bot.modules.catalog_admin.upload_imgbb",
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


async def test_imgbb_key_alone_does_not_write_new_photo_blobs_to_mongo():
    app, store = await setup(imgbb_key="FAKE_HOST_KEY")
    original = jpeg()
    app.stream_media = AsyncMock()
    try:
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock),
            patch(
                "summon.bot.modules.catalog_admin.upload_imgbb", new_callable=AsyncMock
            ) as hosted,
        ):
            await dispatch(app, packet("/addchar A | S | 2", photo=(original, len(original))))
        hosted.assert_not_awaited()
        store.catalog.create_character.assert_not_awaited()
        app.stream_media.assert_not_called()
    finally:
        await app.dispatcher.stop()


async def test_imgbb_mode_still_rejects_protected_media_before_network():
    app, store = await setup(artwork_backend="imgbb", imgbb_key="FAKE_HOST_KEY")
    original = jpeg()
    app.stream_media = AsyncMock()
    try:
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock),
            patch(
                "summon.bot.modules.catalog_admin.upload_imgbb", new_callable=AsyncMock
            ) as hosted,
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


async def test_chance_updates_one_weight_as_private_audited_operator_action():
    app, store = await setup()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet("/chance 14 500"))
        store.catalog.set_spawn_weight.assert_awaited_once_with(
            "amv-edition", 500, actor_id=42, event_id=_event_id(123, 42, 50)
        )
        card = sent.await_args.args[2]
        assert card.title == "Summon weight saved"
        assert card.fields == (("Rarity", "AMV Edition"), ("Weight", "10 → 500"))
        assert "does not change ownership" in card.body
    finally:
        await app.dispatcher.stop()


@pytest.mark.parametrize(
    "text",
    [
        "/chance",
        "/chance 1",
        "/chance 19 5",
        "/chance epic 5",
        "/chance 1 -1",
        "/chance 1 1000001",
        "/chance 1 1e3",
        "/chance 1 2 extra",
        "/chance 1 " + "9" * 100,
    ],
)
async def test_chance_rejects_invalid_values_without_changing_weights(text):
    app, store = await setup()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet(text))
        store.catalog.set_spawn_weight.assert_not_awaited()
        assert sent.await_args.args[2].title == "Set a summon weight"
    finally:
        await app.dispatcher.stop()


async def test_chance_refuses_group_and_non_operator_without_a_write():
    app, store = await setup()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet("/chance 1 100", user_id=7))
        assert sent.await_args.args[2].title == "Operator access required"
        with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private:
            await dispatch(app, packet("/chance 1 100", group=True))
        assert private.await_args.args[3].title == "Use a private chat"
        store.catalog.set_spawn_weight.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_chance_conflicts_and_uncertain_database_results_are_not_retried():
    from summon.repositories.rarity_weights import SpawnWeightConflict

    for failure, title in (
        (SpawnWeightConflict("already processed"), "Weight update not applied"),
        (AutoReconnect("PRIVATE_DATABASE_DETAIL"), "Result unconfirmed"),
    ):
        app, store = await setup()
        store.catalog.set_spawn_weight.side_effect = failure
        try:
            with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
                await dispatch(app, packet("/chance 1 100"))
            store.catalog.set_spawn_weight.assert_awaited_once()
            card = sent.await_args.args[2]
            assert card.title == title
            assert "PRIVATE_DATABASE_DETAIL" not in card.body
            assert "/chancelist" in card.body
        finally:
            await app.dispatcher.stop()


@pytest.mark.parametrize("value", [0, 1_000_000])
async def test_chance_accepts_bounded_zero_and_upper_limit(value):
    app, store = await setup()
    store.catalog.set_spawn_weight.return_value = (1200, value)
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet(f"/chance 3 {value}"))
        assert store.catalog.set_spawn_weight.await_args.args == ("special-edition", value)
        assert sent.await_args.args[2].title == "Summon weight saved"
    finally:
        await app.dispatcher.stop()


@pytest.mark.parametrize("command", ["hclaim", "claim"])
async def test_free_daily_character_native_dispatch_binds_to_telegram_sender(command):
    app, store = await setup()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet(f"/{command}", user_id=7))
        args = store.collection.claim_daily_character.await_args.args
        assert args[0].id == 7 and args[1] == "aiko"
        assert args[2].utcoffset().total_seconds() == 0
        assert sent.await_args.args[2].title == "Daily character claimed"
        assert "for free" in sent.await_args.args[2].body
        assert store.limiter.allow.await_count == 2
    finally:
        await app.dispatcher.stop()


async def test_free_daily_character_group_response_is_private():
    app, store = await setup()
    try:
        with patch("summon.bot.handlers.send_ephemeral_card", new_callable=AsyncMock) as private:
            await dispatch(app, packet("/hclaim", group=True))
        assert private.await_args.args[2] == 42
        assert private.await_args.args[3].title == "Daily character claimed"
        assert store.collection.claim_daily_character.await_args.args[0].id == 42
    finally:
        await app.dispatcher.stop()


async def test_free_daily_character_invalid_arguments_never_choose_or_write():
    app, store = await setup()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet("/hclaim some-other-user"))
        assert "without arguments" in sent.await_args.args[2].body
        store.catalog.choose.assert_not_awaited()
        store.collection.claim_daily_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_daily_character_failures_are_private_and_do_not_retry():
    from summon.repositories.collection import (
        DailyCharacterAlreadyClaimed,
        DailyCharacterUnavailable,
    )

    for error, title in (
        (DailyCharacterAlreadyClaimed(), "Daily character already claimed"),
        (DailyCharacterUnavailable(), "No character is available"),
        (AutoReconnect("PRIVATE_DATABASE_DETAIL"), "Claim unconfirmed"),
    ):
        app, store = await setup()
        store.collection.claim_daily_character.side_effect = error
        try:
            with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
                await dispatch(app, packet("/hclaim"))
            card = sent.await_args.args[2]
            assert card.title == title and "PRIVATE_DATABASE_DETAIL" not in card.body
            store.collection.claim_daily_character.assert_awaited_once()
            if title == "Claim unconfirmed":
                assert "/collection" in card.body
        finally:
            await app.dispatcher.stop()


async def test_free_daily_character_global_limit_prevents_selection():
    app, store = await setup()
    store.limiter.allow.side_effect = [True, False]
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet("/hclaim"))
        assert sent.await_args.args[2].title == "Please slow down"
        store.catalog.choose.assert_not_awaited()
        store.collection.claim_daily_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_free_daily_character_with_no_eligible_catalog_does_not_consume_claim():
    app, store = await setup()
    store.catalog.choose.return_value = None
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet("/hclaim"))
        assert sent.await_args.args[2].title == "No character is available"
        store.collection.claim_daily_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_claimlist_uses_the_same_current_weights_as_daily_characters():
    app, store = await setup()
    store.catalog.spawn_weights.return_value = dict(RARITY_WEIGHTS) | {"rare": 0}
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet("/claimlist"))
        card = sent.await_args.args[2]
        assert card.title == "Summon rarity weights"
        assert "free /hclaim" in card.body
        assert ("Rare", "0") in card.fields
    finally:
        await app.dispatcher.stop()


def callback_packet(data, *, user_id=42):
    actor = raw.types.User(
        id=user_id, first_name="Operator", access_hash=user_id, usernames=[], restriction_reason=[]
    )
    update = raw.types.UpdateBotCallbackQuery(
        query_id=1234,
        user_id=user_id,
        peer=raw.types.PeerUser(user_id=user_id),
        msg_id=51,
        chat_instance=99,
        data=data.encode(),
    )
    return update, {user_id: actor}, {}


async def prepare_upload_callback(app, text, source):
    """Drive native Wzgram message dispatch and retain its actual signed buttons."""
    app.message_cache[(42, 49)] = source
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
        await dispatch(app, packet(text, reply_id=49))
    assert sent.await_args.args[2].title == "Choose a media host"
    buttons = sent.await_args.kwargs["reply_markup"].inline_keyboard[0]
    command = Message(
        id=50,
        chat=Chat(id=42, type=ChatType.PRIVATE),
        from_user=User(id=42),
        date=datetime.now(UTC),
        text=text,
        reply_to_message_id=49,
        reply_to_message=source,
    )
    app.message_cache[(42, 51)] = Message(
        id=51, chat=Chat(id=42, type=ChatType.PRIVATE), from_user=app.me
    )
    app.get_messages = AsyncMock(return_value=command)
    app.answer_callback_query = AsyncMock(return_value=True)
    return buttons, command


async def review_upload_choice(app, button):
    """Choose a host and return the separately signed review controls."""
    with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
        await dispatch(app, callback_packet(button.callback_data))
    card = sent.await_args.args[2]
    assert card.title == "Review before publishing"
    return card, sent.await_args.kwargs["reply_markup"].inline_keyboard[0]


def source_photo(content):
    return Message(
        id=49,
        chat=Chat(id=42, type=ChatType.PRIVATE),
        from_user=User(id=42),
        photo=Photo(
            file_id="original-photo-id",
            file_unique_id="photo-unique",
            width=16,
            height=16,
            file_size=len(content),
            date=datetime.now(UTC),
        ),
    )


async def test_native_photo_provider_choice_saves_host_url_and_archived_file_id():
    app, store = await setup(db_channel=-1001234567890, imgbb_key="FAKE_HOST_KEY")
    content = jpeg()
    source = source_photo(content)
    events = []

    async def stream(message, *, limit):
        assert message.id == 49 and limit == 4
        yield content

    async def host(*_args, **_kwargs):
        events.append("host")
        return "https://i.ibb.co/test/photo.jpg"

    async def channel_post(**kwargs):
        assert kwargs["chat_id"] == -1001234567890
        assert kwargs["photo"].getvalue().startswith(b"\xff\xd8\xff")
        events.append("channel")
        return SimpleNamespace(photo=SimpleNamespace(file_id="channel-photo-id"))

    async def save(data, **kwargs):
        events.append("catalog")
        assert kwargs["event_id"] == _event_id(123, 42, 50)
        return {"id": data.id, "number": 1}

    app.stream_media = stream
    app.get_chat = AsyncMock(return_value=SimpleNamespace(type=ChatType.CHANNEL))
    app.send_photo = AsyncMock(side_effect=channel_post)
    store.catalog.create_character.side_effect = save
    store.limiter.claim_once.side_effect = [True, False, False]
    try:
        buttons, _ = await prepare_upload_callback(
            app, '/upload "Toji Fushiguro" "Jujutsu Kaisen" 2', source
        )
        assert len(buttons) == 2 and "ImgBB" in buttons[1].text
        card, review_buttons = await review_upload_choice(app, buttons[1])
        assert ("Character", "Toji Fushiguro") in card.fields
        assert ("Series", "Jujutsu Kaisen") in card.fields
        assert ("Rarity", "Rare") in card.fields and ("Host", "ImgBB") in card.fields
        assert "Publish publicly" in review_buttons[0].text
        assert "Cancel upload" in review_buttons[1].text
        assert events == []
        store.limiter.claim_once.assert_not_awaited()
        app.get_chat.assert_not_awaited()
        store.catalog.prepare_numbering.assert_not_awaited()
        store.catalog.create_character.assert_not_awaited()
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.modules.catalog_admin.upload_imgbb",
                new_callable=AsyncMock,
                side_effect=host,
            ) as hosted,
        ):
            await dispatch(app, callback_packet(review_buttons[0].callback_data))
            await dispatch(app, callback_packet(review_buttons[0].callback_data))
            await dispatch(app, callback_packet(review_buttons[1].callback_data))
        assert app.answer_callback_query.await_args.kwargs["show_alert"] is True
        data = store.catalog.create_character.await_args.args[0]
        assert (data.name, data.category, data.rarity) == (
            "Toji Fushiguro",
            "Jujutsu Kaisen",
            "rare",
        )
        assert data.image_url == "https://i.ibb.co/test/photo.jpg"
        assert data.telegram_file_id == "channel-photo-id" and data.media_type == "photo"
        assert "artwork" not in store.catalog.create_character.await_args.kwargs
        assert events == ["host", "channel", "catalog"]
        hosted.assert_awaited_once()
        store.catalog.create_character.assert_awaited_once()
        assert sent.await_args.args[2].title == "Character added"
        assert store.limiter.claim_once.await_count == 3
    finally:
        await app.dispatcher.stop()


async def test_native_video_upload_uses_catbox_and_forces_amv():
    from wzgram.types import Video

    content = b"\x00\x00\x00\x18ftypisom" + b"video"
    source = Message(
        id=49,
        chat=Chat(id=42, type=ChatType.PRIVATE),
        from_user=User(id=42),
        video=Video(
            file_id="original-video-id",
            file_unique_id="video-unique",
            width=16,
            height=16,
            codec="h264",
            duration=1,
            mime_type="video/mp4",
            file_size=len(content),
        ),
    )
    app, store = await setup(db_channel=-1001234567890, imgbb_key="FAKE_HOST_KEY")

    async def stream(_message, *, limit):
        assert limit == 17
        yield content

    app.stream_media = stream
    app.get_chat = AsyncMock(return_value=SimpleNamespace(type=ChatType.CHANNEL))
    app.send_video = AsyncMock(
        return_value=SimpleNamespace(video=SimpleNamespace(file_id="channel-video-id"))
    )
    store.catalog.create_character.return_value = {"id": "generated", "number": 1}
    try:
        buttons, _ = await prepare_upload_callback(
            app, "/upload Toji-Fushiguro Jujutsu-Kaisen 2", source
        )
        assert len(buttons) == 1 and "Catbox" in buttons[0].text
        card, review_buttons = await review_upload_choice(app, buttons[0])
        assert ("Rarity", "AMV Edition") in card.fields and ("Host", "Catbox") in card.fields
        assert next(value for label, value in card.fields if label == "Media").startswith("Video")
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.modules.catalog_admin.upload_catbox",
                new_callable=AsyncMock,
                return_value="https://files.catbox.moe/example.mp4",
            ) as hosted,
        ):
            await dispatch(app, callback_packet(review_buttons[0].callback_data))
        hosted.assert_awaited_once()
        data = store.catalog.create_character.await_args.args[0]
        assert (data.name, data.category, data.rarity, data.media_type) == (
            "Toji Fushiguro",
            "Jujutsu Kaisen",
            "amv-edition",
            "video",
        )
        assert data.image_url == "https://files.catbox.moe/example.mp4"
        assert data.telegram_file_id == "channel-video-id"
        assert sent.await_args.args[2].title == "Character added"
    finally:
        await app.dispatcher.stop()


@pytest.mark.parametrize("phase", ["choice", "publish", "cancel"])
@pytest.mark.parametrize("change", ["actor", "provider", "text", "media", "size"])
async def test_native_upload_menu_rejects_other_actor_or_changed_signed_inputs(change, phase):
    app, store = await setup(
        operators=(42, 99), db_channel=-1001234567890, imgbb_key="FAKE_HOST_KEY"
    )
    content = jpeg()
    source = source_photo(content)
    app.stream_media = AsyncMock()
    try:
        buttons, command = await prepare_upload_callback(app, "/upload A | S | 2", source)
        callback = buttons[0].callback_data
        if phase != "choice":
            _, review_buttons = await review_upload_choice(app, buttons[0])
            callback = review_buttons[0 if phase == "publish" else 1].callback_data
        user_id = 42
        if change == "actor":
            user_id = 99
            app.message_cache[(99, 51)] = Message(id=51, chat=Chat(id=99, type=ChatType.PRIVATE))
        elif change == "provider":
            callback = callback.replace(":c:", ":i:")
        elif change == "text":
            command.text = "/upload Changed | S | 2"
        elif change == "size":
            source.photo.file_size += 1
        else:
            source.photo.file_unique_id = "different-photo"
        with patch(
            "summon.bot.modules.catalog_admin.upload_catbox", new_callable=AsyncMock
        ) as hosted:
            await dispatch(app, callback_packet(callback, user_id=user_id))
        hosted.assert_not_awaited()
        app.stream_media.assert_not_called()
        store.limiter.claim_once.assert_not_awaited()
        store.catalog.create_character.assert_not_awaited()
        assert app.answer_callback_query.await_args.kwargs["show_alert"] is True
    finally:
        await app.dispatcher.stop()


async def test_native_channel_timeout_keeps_original_file_id_and_warns_without_retry():
    app, store = await setup(db_channel=-1001234567890)
    content = jpeg()
    source = source_photo(content)

    async def stream(_message, *, limit):
        yield content

    app.stream_media = stream
    app.get_chat = AsyncMock(return_value=SimpleNamespace(type=ChatType.CHANNEL))
    app.send_photo = AsyncMock(side_effect=TimeoutError("PRIVATE_ARCHIVE_DETAIL"))
    store.catalog.create_character.return_value = {"id": "generated", "number": 1}
    try:
        buttons, _ = await prepare_upload_callback(app, "/upload A | S | 1", source)
        _, review_buttons = await review_upload_choice(app, buttons[0])
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.modules.catalog_admin.upload_catbox",
                new_callable=AsyncMock,
                return_value="https://files.catbox.moe/example.jpg",
            ),
        ):
            await dispatch(app, callback_packet(review_buttons[0].callback_data))
        assert (
            store.catalog.create_character.await_args.args[0].telegram_file_id
            == "original-photo-id"
        )
        assert "Channel copy unconfirmed" in sent.await_args.args[2].body
        app.send_photo.assert_awaited_once()
    finally:
        await app.dispatcher.stop()


async def test_native_gif_upload_uses_animation_file_id_and_amv_rarity():
    from wzgram.types import Animation

    content = b"GIF89a" + b"frames"
    source = Message(
        id=49,
        chat=Chat(id=42, type=ChatType.PRIVATE),
        from_user=User(id=42),
        animation=Animation(
            file_id="original-gif-id",
            file_unique_id="gif-unique",
            width=16,
            height=16,
            duration=1,
            mime_type="image/gif",
            file_size=len(content),
        ),
    )
    app, store = await setup(db_channel=-1001234567890, imgbb_key="FAKE_HOST_KEY")

    async def stream(_message, *, limit):
        yield content

    app.stream_media = stream
    app.get_chat = AsyncMock(return_value=SimpleNamespace(type=ChatType.CHANNEL))
    app.send_animation = AsyncMock(
        return_value=SimpleNamespace(animation=SimpleNamespace(file_id="channel-gif-id"))
    )
    store.catalog.create_character.return_value = {"id": "generated", "number": 1}
    try:
        buttons, _ = await prepare_upload_callback(app, '/upload "A Name" "A Series" 8', source)
        assert len(buttons) == 1 and "Catbox" in buttons[0].text
        card, review_buttons = await review_upload_choice(app, buttons[0])
        assert ("Rarity", "AMV Edition") in card.fields
        assert next(value for label, value in card.fields if label == "Media").startswith("GIF")
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.modules.catalog_admin.upload_catbox",
                new_callable=AsyncMock,
                return_value="https://files.catbox.moe/example.gif",
            ),
        ):
            await dispatch(app, callback_packet(review_buttons[0].callback_data))
        data = store.catalog.create_character.await_args.args[0]
        assert data.rarity == "amv-edition" and data.media_type == "animation"
        assert data.telegram_file_id == "channel-gif-id"
        assert data.image_url.endswith(".gif")
        assert sent.await_args.args[2].title == "Character added"
    finally:
        await app.dispatcher.stop()


@pytest.mark.parametrize(
    ("button", "replacement"),
    [
        ("choose", "catalog-publish"),
        ("choose", "catalog-cancel"),
        ("publish", "catalog-cancel"),
        ("cancel", "catalog-publish"),
    ],
)
async def test_upload_action_tokens_cannot_be_reused_for_another_phase(button, replacement):
    app, store = await setup(db_channel=-1001234567890)
    app.stream_media = AsyncMock()
    try:
        buttons, _ = await prepare_upload_callback(app, "/upload A | S | 1", source_photo(jpeg()))
        _, review_buttons = await review_upload_choice(app, buttons[0])
        original = {
            "choose": buttons[0].callback_data,
            "publish": review_buttons[0].callback_data,
            "cancel": review_buttons[1].callback_data,
        }[button]
        forged = original.replace(original.split(":", 1)[0], replacement, 1)
        with patch(
            "summon.bot.modules.catalog_admin.upload_catbox", new_callable=AsyncMock
        ) as host:
            await dispatch(app, callback_packet(forged))
        assert app.answer_callback_query.await_args.kwargs["show_alert"] is True
        app.stream_media.assert_not_called()
        host.assert_not_awaited()
        store.limiter.claim_once.assert_not_awaited()
        store.catalog.prepare_numbering.assert_not_awaited()
        store.catalog.create_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_cancel_blocks_every_provider_review_without_external_side_effects():
    app, store = await setup(db_channel=-1001234567890, imgbb_key="FAKE_HOST_KEY")
    app.stream_media = AsyncMock()
    app.get_chat = AsyncMock()
    claims = set()

    async def claim(key, *, seconds):
        assert seconds == 7 * 86400
        if key in claims:
            return False
        claims.add(key)
        return True

    store.limiter.claim_once.side_effect = claim
    try:
        buttons, _ = await prepare_upload_callback(app, "/upload A | S | 1", source_photo(jpeg()))
        _, catbox_review = await review_upload_choice(app, buttons[0])
        _, imgbb_review = await review_upload_choice(app, buttons[1])
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.modules.catalog_admin.upload_catbox", new_callable=AsyncMock
            ) as catbox,
            patch("summon.bot.modules.catalog_admin.upload_imgbb", new_callable=AsyncMock) as imgbb,
        ):
            await dispatch(app, callback_packet(imgbb_review[1].callback_data))
            assert sent.await_args.args[2].title == "Upload canceled"
            assert "No media was uploaded" in sent.await_args.args[2].body
            await dispatch(app, callback_packet(catbox_review[0].callback_data))
            assert app.answer_callback_query.await_args.kwargs["show_alert"] is True
            await dispatch(app, callback_packet(imgbb_review[0].callback_data))
            assert app.answer_callback_query.await_args.kwargs["show_alert"] is True
        assert claims == {f"catalog-media:{_event_id(123, 42, 50)}"}
        catbox.assert_not_awaited()
        imgbb.assert_not_awaited()
        app.stream_media.assert_not_called()
        app.get_chat.assert_not_awaited()
        store.catalog.prepare_numbering.assert_not_awaited()
        store.catalog.create_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_expired_review_never_claims_or_publishes():
    from datetime import timedelta

    app, store = await setup(db_channel=-1001234567890)
    app.stream_media = AsyncMock()
    try:
        buttons, command = await prepare_upload_callback(
            app, "/upload A | S | 1", source_photo(jpeg())
        )
        _, review = await review_upload_choice(app, buttons[0])
        command.date = datetime.now(UTC) - timedelta(minutes=16)
        with patch(
            "summon.bot.modules.catalog_admin.upload_catbox", new_callable=AsyncMock
        ) as host:
            await dispatch(app, callback_packet(review[0].callback_data))
        assert app.answer_callback_query.await_args.kwargs["show_alert"] is True
        host.assert_not_awaited()
        app.stream_media.assert_not_called()
        store.limiter.claim_once.assert_not_awaited()
        store.catalog.create_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


@pytest.mark.parametrize("phase", ["publish", "cancel"])
async def test_uncertain_upload_claim_never_starts_external_io(phase):
    app, store = await setup(db_channel=-1001234567890)
    app.stream_media = AsyncMock()
    store.limiter.claim_once.side_effect = AutoReconnect("PRIVATE_DATABASE_DETAIL")
    try:
        buttons, _ = await prepare_upload_callback(app, "/upload A | S | 1", source_photo(jpeg()))
        _, review = await review_upload_choice(app, buttons[0])
        with patch(
            "summon.bot.modules.catalog_admin.upload_catbox", new_callable=AsyncMock
        ) as host:
            await dispatch(
                app, callback_packet(review[0 if phase == "publish" else 1].callback_data)
            )
        args = app.answer_callback_query.await_args.kwargs
        assert args["show_alert"] is True
        assert (
            "unconfirmed" in args["text"].lower() and "PRIVATE_DATABASE_DETAIL" not in args["text"]
        )
        host.assert_not_awaited()
        app.stream_media.assert_not_called()
        store.catalog.prepare_numbering.assert_not_awaited()
        store.catalog.create_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_upload_conflict_after_review_stops_before_claim_or_external_io():
    app, store = await setup(db_channel=-1001234567890)
    app.stream_media = AsyncMock()
    # Another operator creates this internal ID after it was reviewed.
    store.catalog.character_exists.side_effect = [False, True]
    try:
        buttons, _ = await prepare_upload_callback(app, "/upload A | S | 1", source_photo(jpeg()))
        _, review = await review_upload_choice(app, buttons[0])
        with patch(
            "summon.bot.modules.catalog_admin.upload_catbox", new_callable=AsyncMock
        ) as host:
            await dispatch(app, callback_packet(review[0].callback_data))
        assert app.answer_callback_query.await_args.kwargs["show_alert"] is True
        assert "already exists" in app.answer_callback_query.await_args.kwargs["text"]
        store.limiter.claim_once.assert_not_awaited()
        store.catalog.prepare_numbering.assert_not_awaited()
        app.stream_media.assert_not_called()
        host.assert_not_awaited()
        store.catalog.create_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_numbering_failure_after_claim_cannot_start_media_upload():
    from summon.repositories.character_numbers import CharacterNumberingUnavailable

    app, store = await setup(db_channel=-1001234567890)
    app.stream_media = AsyncMock()
    store.catalog.prepare_numbering.side_effect = CharacterNumberingUnavailable("PRIVATE_DETAIL")
    try:
        buttons, _ = await prepare_upload_callback(app, "/upload A | S | 1", source_photo(jpeg()))
        _, review = await review_upload_choice(app, buttons[0])
        with patch(
            "summon.bot.modules.catalog_admin.upload_catbox", new_callable=AsyncMock
        ) as host:
            await dispatch(app, callback_packet(review[0].callback_data))
        args = app.answer_callback_query.await_args.kwargs
        assert args["show_alert"] is True and "No upload started" in args["text"]
        assert "new /upload" in args["text"] and "PRIVATE_DETAIL" not in args["text"]
        store.limiter.claim_once.assert_awaited_once()
        app.stream_media.assert_not_called()
        host.assert_not_awaited()
        store.catalog.create_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_review_unavailable_when_catalog_preflight_fails():
    app, store = await setup(db_channel=-1001234567890)
    app.stream_media = AsyncMock()
    store.catalog.character_exists.side_effect = AutoReconnect("PRIVATE_DETAIL")
    try:
        buttons, _ = await prepare_upload_callback(app, "/upload A | S | 1", source_photo(jpeg()))
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, callback_packet(buttons[0].callback_data))
        assert app.answer_callback_query.await_args.kwargs["show_alert"] is True
        assert "No new upload was started" in app.answer_callback_query.await_args.kwargs["text"]
        assert "PRIVATE_DETAIL" not in app.answer_callback_query.await_args.kwargs["text"]
        sent.assert_not_awaited()
        store.limiter.claim_once.assert_not_awaited()
        store.catalog.prepare_numbering.assert_not_awaited()
        app.stream_media.assert_not_called()
        store.catalog.create_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_refreshed_telegram_file_reference_keeps_review_valid():
    app, store = await setup(db_channel=-1001234567890)
    content = jpeg()
    source = source_photo(content)

    async def stream(_message, *, limit):
        yield content

    app.stream_media = stream
    app.get_chat = AsyncMock(return_value=SimpleNamespace(type=ChatType.CHANNEL))
    app.send_photo = AsyncMock(return_value=SimpleNamespace(photo=None))
    store.catalog.create_character.return_value = {"id": "generated", "number": 1}
    try:
        buttons, _ = await prepare_upload_callback(app, "/upload A | S | 1", source)
        source.photo.file_id = "renewed-reference-for-review"
        _, review = await review_upload_choice(app, buttons[0])
        source.photo.file_id = "renewed-reference-for-publish"
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock),
            patch(
                "summon.bot.modules.catalog_admin.upload_catbox",
                new_callable=AsyncMock,
                return_value="https://files.catbox.moe/example.jpg",
            ) as host,
        ):
            await dispatch(app, callback_packet(review[0].callback_data))
        host.assert_awaited_once()
        assert (
            store.catalog.create_character.await_args.args[0].telegram_file_id
            == "renewed-reference-for-publish"
        )
    finally:
        await app.dispatcher.stop()


@pytest.mark.parametrize(
    ("user_id", "group", "expected"), [(42, False, True), (99, False, False), (42, True, False)]
)
async def test_upload_review_instructions_appear_only_in_private_operator_help(
    user_id, group, expected
):
    app, _ = await setup()
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet("/help", user_id=user_id, group=group))
        fields = dict(sent.await_args.args[2].fields)
        assert ("Media upload" in fields) is expected
        if expected:
            assert "/upload" in fields["Media upload"]
            assert "/updatechar exact-id media" in fields["Media upload"]
            assert "review" in fields["Media upload"] and "cancel" in fields["Media upload"]
    finally:
        await app.dispatcher.stop()


async def test_operator_revocation_after_review_prevents_publication():
    app, store = await setup(db_channel=-1001234567890)
    app.stream_media = AsyncMock()
    try:
        buttons, _ = await prepare_upload_callback(app, "/upload A | S | 1", source_photo(jpeg()))
        _, review = await review_upload_choice(app, buttons[0])
        app._test_settings.operator_ids = []
        with patch(
            "summon.bot.modules.catalog_admin.upload_catbox", new_callable=AsyncMock
        ) as host:
            await dispatch(app, callback_packet(review[0].callback_data))
        assert app.answer_callback_query.await_args.kwargs["show_alert"] is True
        store.limiter.claim_once.assert_not_awaited()
        store.catalog.prepare_numbering.assert_not_awaited()
        store.catalog.create_character.assert_not_awaited()
        host.assert_not_awaited()
        app.stream_media.assert_not_called()
    finally:
        await app.dispatcher.stop()


def replacement_state(revision="a" * 64, *, rarity="epic", media_type="video", active=True):
    return {
        "revision": revision,
        "character": {
            "id": "aiko",
            "name": "Aiko",
            "category": "Sky",
            "rarity": rarity,
            "media_type": media_type,
            "image_url": "https://files.catbox.moe/old.mp4",
            "description": "Do not overwrite",
            "active": active,
            "number": 7,
        },
    }


async def test_reviewed_photo_replacement_preserves_id_and_waits_for_confirmation():
    app, store = await setup(db_channel=-1001234567890, imgbb_key="FAKE_HOST_KEY")
    content = jpeg()
    source = source_photo(content)
    events = []

    async def stream(_message, *, limit):
        events.append("download")
        yield content

    async def host(*_args, **_kwargs):
        events.append("host")
        return "https://i.ibb.co/test/new.jpg"

    async def channel_post(**_kwargs):
        events.append("channel")
        return SimpleNamespace(photo=SimpleNamespace(file_id="replacement-channel-id"))

    async def save(*_args, **_kwargs):
        events.append("catalog")
        return {"id": "aiko", "number": 7}

    app.stream_media = stream
    app.get_chat = AsyncMock(return_value=SimpleNamespace(type=ChatType.CHANNEL))
    app.send_photo = AsyncMock(side_effect=channel_post)
    store.catalog.edit_state.return_value = replacement_state(active=False)
    store.catalog.replace_media_reviewed.side_effect = save
    try:
        buttons, _ = await prepare_upload_callback(app, "/updatechar aiko media", source)
        assert len(buttons) == 2
        assert events == []
        card, review = await review_upload_choice(app, buttons[1])
        fields = dict(card.fields)
        assert fields["ID"] == "aiko" and fields["Character"] == "Aiko"
        assert fields["Current rarity"] == fields["New rarity"] == "Epic (legacy)"
        assert fields["Current media"] == "Video" and fields["New media"].startswith("Photo")
        assert fields["Availability"] == "Inactive" and fields["Host"] == "ImgBB"
        assert "not deleted" in card.body and "ownership" in card.body
        assert events == []
        store.limiter.claim_once.assert_not_awaited()
        store.catalog.replace_media_reviewed.assert_not_awaited()
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.modules.catalog_admin.upload_imgbb",
                new_callable=AsyncMock,
                side_effect=host,
            ) as hosted,
        ):
            await dispatch(app, callback_packet(review[0].callback_data))
        assert events == ["download", "host", "channel", "catalog"]
        hosted.assert_awaited_once()
        args = store.catalog.replace_media_reviewed.await_args
        assert args.args[0] == "aiko"
        media = args.args[1]
        assert (media.image_url, media.media_type, media.telegram_file_id) == (
            "https://i.ibb.co/test/new.jpg",
            "photo",
            "replacement-channel-id",
        )
        assert args.kwargs["actor_id"] == 42 and args.kwargs["event_id"] == _event_id(123, 42, 50)
        assert args.kwargs["expected_revision"] == "a" * 64
        assert len(args.kwargs["revision_secret"]) == 64
        assert sent.await_args.args[2].title == "Character artwork replaced"
        assert ("Rarity", "Epic (legacy)") in sent.await_args.args[2].fields
        store.catalog.create_character.assert_not_awaited()
        store.catalog.update_character.assert_not_awaited()
        store.catalog.prepare_numbering.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_reviewed_video_replacement_shows_amv_and_keeps_original_id_on_archive_failure():
    from wzgram.types import Video

    content = b"\x00\x00\x00\x18ftypisom" + b"video"
    source = Message(
        id=49,
        chat=Chat(id=42, type=ChatType.PRIVATE),
        from_user=User(id=42),
        video=Video(
            file_id="original-video",
            file_unique_id="stable-video",
            width=16,
            height=16,
            codec="h264",
            duration=1,
            mime_type="video/mp4",
            file_size=len(content),
        ),
    )
    app, store = await setup(db_channel=-1001234567890, imgbb_key="FAKE_HOST_KEY")
    store.catalog.edit_state.return_value = replacement_state(rarity="rare", media_type="photo")
    store.catalog.replace_media_reviewed.return_value = {"id": "aiko", "number": 7}

    async def stream(_message, *, limit):
        yield content

    app.stream_media = stream
    app.get_chat = AsyncMock(return_value=SimpleNamespace(type=ChatType.CHANNEL))
    app.send_video = AsyncMock(side_effect=TimeoutError("private channel detail"))
    try:
        buttons, _ = await prepare_upload_callback(app, "/update aiko media", source)
        assert len(buttons) == 1 and "Catbox" in buttons[0].text
        card, review = await review_upload_choice(app, buttons[0])
        fields = dict(card.fields)
        assert fields["Current rarity"] == "Rare" and fields["New rarity"] == "AMV Edition"
        assert fields["Current media"] == "Photo" and fields["New media"].startswith("Video")
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.modules.catalog_admin.upload_catbox",
                new_callable=AsyncMock,
                return_value="https://files.catbox.moe/new.mp4",
            ) as host,
        ):
            await dispatch(app, callback_packet(review[0].callback_data))
        host.assert_awaited_once()
        media = store.catalog.replace_media_reviewed.await_args.args[1]
        assert media.media_type == "video" and media.telegram_file_id == "original-video"
        assert sent.await_args.args[2].title == "Character artwork replaced"
        assert "Channel copy unconfirmed" in sent.await_args.args[2].body
        assert ("Rarity", "AMV Edition") in sent.await_args.args[2].fields
        app.send_video.assert_awaited_once()
        store.catalog.create_character.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_replacement_stale_before_upload_requires_new_review_without_claim():
    app, store = await setup(db_channel=-1001234567890)
    app.stream_media = AsyncMock()
    store.catalog.edit_state.side_effect = [
        replacement_state("a" * 64),
        replacement_state("b" * 64),
    ]
    try:
        buttons, _ = await prepare_upload_callback(
            app, "/updatechar aiko media", source_photo(jpeg())
        )
        _, review = await review_upload_choice(app, buttons[0])
        with patch(
            "summon.bot.modules.catalog_admin.upload_catbox", new_callable=AsyncMock
        ) as host:
            await dispatch(app, callback_packet(review[0].callback_data))
        args = app.answer_callback_query.await_args.kwargs
        assert args["show_alert"] is True and "review" in args["text"].lower()
        store.limiter.claim_once.assert_not_awaited()
        store.catalog.replace_media_reviewed.assert_not_awaited()
        app.stream_media.assert_not_called()
        host.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_replacement_stale_after_hosting_warns_about_public_orphans_and_never_retries():
    from summon.repositories.catalog import CatalogStaleRevision

    app, store = await setup(db_channel=-1001234567890)
    content = jpeg()
    source = source_photo(content)
    store.catalog.edit_state.return_value = replacement_state()
    store.catalog.replace_media_reviewed.side_effect = CatalogStaleRevision("changed")
    store.limiter.claim_once.side_effect = [True, False]

    async def stream(_message, *, limit):
        yield content

    app.stream_media = stream
    app.get_chat = AsyncMock(return_value=SimpleNamespace(type=ChatType.CHANNEL))
    app.send_photo = AsyncMock(
        return_value=SimpleNamespace(photo=SimpleNamespace(file_id="channel-id"))
    )
    try:
        buttons, _ = await prepare_upload_callback(app, "/updatechar aiko media", source)
        _, review = await review_upload_choice(app, buttons[0])
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.modules.catalog_admin.upload_catbox",
                new_callable=AsyncMock,
                return_value="https://files.catbox.moe/new.jpg",
            ) as host,
        ):
            await dispatch(app, callback_packet(review[0].callback_data))
            card = sent.await_args.args[2]
            assert card.title == "Replacement not applied"
            assert "No replacement was saved" in card.body and "may contain a copy" in card.body
            await dispatch(app, callback_packet(review[0].callback_data))
        host.assert_awaited_once()
        app.send_photo.assert_awaited_once()
        store.catalog.replace_media_reviewed.assert_awaited_once()
        assert app.answer_callback_query.await_args.kwargs["show_alert"] is True
    finally:
        await app.dispatcher.stop()


async def test_canceling_replacement_ignores_later_catalog_edits_and_blocks_old_review():
    app, store = await setup(db_channel=-1001234567890)
    app.stream_media = AsyncMock()
    store.catalog.edit_state.return_value = replacement_state()
    store.limiter.claim_once.side_effect = [True, False]
    try:
        buttons, _ = await prepare_upload_callback(
            app, "/updatechar aiko media", source_photo(jpeg())
        )
        _, review = await review_upload_choice(app, buttons[0])
        store.catalog.edit_state.return_value = replacement_state("b" * 64)
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch("summon.bot.modules.catalog_admin.upload_catbox", new_callable=AsyncMock) as host,
        ):
            await dispatch(app, callback_packet(review[1].callback_data))
            assert sent.await_args.args[2].title == "Upload canceled"
            # Cancel uses the command's shared claim, not the old catalog revision.
            assert store.catalog.edit_state.await_count == 1
            store.catalog.edit_state.return_value = replacement_state()
            await dispatch(app, callback_packet(review[0].callback_data))
        assert app.answer_callback_query.await_args.kwargs["show_alert"] is True
        assert store.limiter.claim_once.await_count == 2
        host.assert_not_awaited()
        app.stream_media.assert_not_called()
        store.catalog.replace_media_reviewed.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


@pytest.mark.parametrize(
    "broken_state",
    [
        {"revision": "a" * 64, "character": None},
        {"revision": "invalid", "character": replacement_state()["character"]},
        {"revision": "a" * 64, "character": {**replacement_state()["character"], "id": "other"}},
        {"revision": "a" * 64, "character": {**replacement_state()["character"], "rarity": "bad"}},
        {"revision": "a" * 64, "character": {**replacement_state()["character"], "active": "yes"}},
        {"revision": "a" * 64, "character": {**replacement_state()["character"], "media_type": []}},
    ],
)
async def test_replacement_unavailable_for_missing_or_invalid_catalog_record(broken_state):
    app, store = await setup(db_channel=-1001234567890)
    app.stream_media = AsyncMock()
    store.catalog.edit_state.return_value = broken_state
    try:
        buttons, _ = await prepare_upload_callback(
            app, "/updatechar aiko media", source_photo(jpeg())
        )
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch("summon.bot.modules.catalog_admin.upload_catbox", new_callable=AsyncMock) as host,
        ):
            await dispatch(app, callback_packet(buttons[0].callback_data))
        assert app.answer_callback_query.await_args.kwargs["show_alert"] is True
        sent.assert_not_awaited()
        host.assert_not_awaited()
        app.stream_media.assert_not_called()
        store.limiter.claim_once.assert_not_awaited()
        store.catalog.replace_media_reviewed.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_reviewed_gif_replacement_publishes_animation_reference_as_amv():
    from wzgram.types import Animation

    content = b"GIF89a" + b"frame"
    source = Message(
        id=49,
        chat=Chat(id=42, type=ChatType.PRIVATE),
        from_user=User(id=42),
        animation=Animation(
            file_id="original-gif",
            file_unique_id="stable-gif",
            width=16,
            height=16,
            duration=1,
            mime_type="image/gif",
            file_size=len(content),
        ),
    )
    app, store = await setup(db_channel=-1001234567890, imgbb_key="FAKE_HOST_KEY")
    store.catalog.edit_state.return_value = replacement_state(rarity="rare", media_type="photo")
    store.catalog.replace_media_reviewed.return_value = {"id": "aiko", "number": 7}

    async def stream(_message, *, limit):
        yield content

    app.stream_media = stream
    app.get_chat = AsyncMock(return_value=SimpleNamespace(type=ChatType.CHANNEL))
    app.send_animation = AsyncMock(
        return_value=SimpleNamespace(animation=SimpleNamespace(file_id="channel-gif"))
    )
    try:
        buttons, _ = await prepare_upload_callback(app, "/updatechar aiko media", source)
        assert len(buttons) == 1 and "Catbox" in buttons[0].text
        card, review = await review_upload_choice(app, buttons[0])
        assert dict(card.fields)["New rarity"] == "AMV Edition"
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.modules.catalog_admin.upload_catbox",
                new_callable=AsyncMock,
                return_value="https://files.catbox.moe/new.gif",
            ) as host,
        ):
            await dispatch(app, callback_packet(review[0].callback_data))
        host.assert_awaited_once()
        media = store.catalog.replace_media_reviewed.await_args.args[1]
        assert media.media_type == "animation" and media.telegram_file_id == "channel-gif"
        assert media.image_url.endswith(".gif")
        assert sent.await_args.args[2].title == "Character artwork replaced"
        assert ("Rarity", "AMV Edition") in sent.await_args.args[2].fields
    finally:
        await app.dispatcher.stop()


async def test_replacement_deleted_after_review_can_still_be_canceled_without_io():
    app, store = await setup(db_channel=-1001234567890)
    app.stream_media = AsyncMock()
    store.catalog.edit_state.return_value = replacement_state()
    try:
        buttons, _ = await prepare_upload_callback(
            app, "/updatechar aiko media", source_photo(jpeg())
        )
        _, review = await review_upload_choice(app, buttons[0])
        store.catalog.edit_state.return_value = {"revision": "b" * 64, "character": None}
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch("summon.bot.modules.catalog_admin.upload_catbox", new_callable=AsyncMock) as host,
        ):
            await dispatch(app, callback_packet(review[0].callback_data))
            assert app.answer_callback_query.await_args.kwargs["show_alert"] is True
            assert "no longer exists" in app.answer_callback_query.await_args.kwargs["text"]
            store.limiter.claim_once.assert_not_awaited()
            await dispatch(app, callback_packet(review[1].callback_data))
        assert sent.await_args.args[2].title == "Upload canceled"
        # Only choose and attempted publish consulted the record, never cancel.
        assert store.catalog.edit_state.await_count == 2
        store.limiter.claim_once.assert_awaited_once()
        store.catalog.replace_media_reviewed.assert_not_awaited()
        app.stream_media.assert_not_called()
        host.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_tampered_replacement_review_cannot_claim_or_upload():
    app, store = await setup(db_channel=-1001234567890)
    app.stream_media = AsyncMock()
    store.catalog.edit_state.return_value = replacement_state()
    try:
        buttons, _ = await prepare_upload_callback(
            app, "/updatechar aiko media", source_photo(jpeg())
        )
        _, review = await review_upload_choice(app, buttons[0])
        data = review[0].callback_data
        forged = data[:-1] + ("A" if data[-1] != "A" else "B")
        with patch(
            "summon.bot.modules.catalog_admin.upload_catbox", new_callable=AsyncMock
        ) as host:
            await dispatch(app, callback_packet(forged))
        assert app.answer_callback_query.await_args.kwargs["show_alert"] is True
        store.limiter.claim_once.assert_not_awaited()
        store.catalog.replace_media_reviewed.assert_not_awaited()
        app.stream_media.assert_not_called()
        host.assert_not_awaited()
    finally:
        await app.dispatcher.stop()


async def test_replacement_host_timeout_is_unconfirmed_and_button_is_not_retried():
    app, store = await setup(db_channel=-1001234567890)
    content = jpeg()
    store.catalog.edit_state.return_value = replacement_state()
    store.limiter.claim_once.side_effect = [True, False]

    async def stream(_message, *, limit):
        yield content

    app.stream_media = stream
    app.get_chat = AsyncMock()
    try:
        buttons, _ = await prepare_upload_callback(
            app, "/updatechar aiko media", source_photo(content)
        )
        _, review = await review_upload_choice(app, buttons[0])
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.modules.catalog_admin.upload_catbox",
                new_callable=AsyncMock,
                side_effect=TimeoutError("synthetic provider detail"),
            ) as host,
        ):
            await dispatch(app, callback_packet(review[0].callback_data))
            card = sent.await_args.args[2]
            assert card.title == "Upload result unconfirmed" and "may already exist" in card.body
            await dispatch(app, callback_packet(review[0].callback_data))
        host.assert_awaited_once()
        app.get_chat.assert_not_awaited()
        store.catalog.replace_media_reviewed.assert_not_awaited()
        assert app.answer_callback_query.await_args.kwargs["show_alert"] is True
    finally:
        await app.dispatcher.stop()


async def test_stale_replacement_can_be_reviewed_again_before_its_command_expires():
    app, store = await setup(db_channel=-1001234567890)
    content = jpeg()
    store.catalog.edit_state.side_effect = [
        replacement_state("a" * 64),
        replacement_state("b" * 64, rarity="rare"),
        replacement_state("b" * 64, rarity="rare"),
        replacement_state("b" * 64, rarity="rare"),
    ]
    store.catalog.replace_media_reviewed.return_value = {"id": "aiko", "number": 7}

    async def stream(_message, *, limit):
        yield content

    app.stream_media = stream
    app.get_chat = AsyncMock(return_value=SimpleNamespace(type=ChatType.CHANNEL))
    app.send_photo = AsyncMock(
        return_value=SimpleNamespace(photo=SimpleNamespace(file_id="channel"))
    )
    try:
        buttons, _ = await prepare_upload_callback(
            app, "/updatechar aiko media", source_photo(content)
        )
        _, first = await review_upload_choice(app, buttons[0])
        await dispatch(app, callback_packet(first[0].callback_data))
        assert app.answer_callback_query.await_args.kwargs["show_alert"] is True
        store.limiter.claim_once.assert_not_awaited()
        card, fresh = await review_upload_choice(app, buttons[0])
        assert dict(card.fields)["Current rarity"] == "Rare"
        assert fresh[0].callback_data != first[0].callback_data
        with (
            patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent,
            patch(
                "summon.bot.modules.catalog_admin.upload_catbox",
                new_callable=AsyncMock,
                return_value="https://files.catbox.moe/new.jpg",
            ) as host,
        ):
            await dispatch(app, callback_packet(fresh[0].callback_data))
        assert sent.await_args.args[2].title == "Character artwork replaced"
        assert (
            store.catalog.replace_media_reviewed.await_args.kwargs["expected_revision"] == "b" * 64
        )
        host.assert_awaited_once()
    finally:
        await app.dispatcher.stop()


@pytest.mark.parametrize("command", ["/search", "/find", "/sips"])
async def test_native_wzgram_reference_search_aliases_offer_actor_bound_pages(command):
    app, store = await setup()
    store.catalog.list = AsyncMock(
        return_value={
            "items": [
                {"id": "aiko", "number": 1, "name": "Aiko", "category": "Sky", "rarity": "rare"}
            ],
            "total": 11,
        }
    )
    text = f"{command} Aiko"
    try:
        with patch("summon.bot.handlers.send_card", new_callable=AsyncMock) as sent:
            await dispatch(app, packet(text))
        assert sent.await_args.args[2].title == "Character search"
        buttons = sent.await_args.kwargs["reply_markup"].inline_keyboard[0]
        assert len(buttons) == 1 and buttons[0].callback_data.startswith("catalog-search:")
        assert "Aiko" not in buttons[0].callback_data
        original = Message(
            id=50,
            chat=Chat(id=42, type=ChatType.PRIVATE),
            from_user=User(id=42),
            date=datetime.now(UTC),
            text=text,
        )
        app.message_cache[(42, 51)] = Message(
            id=51, chat=Chat(id=42, type=ChatType.PRIVATE), from_user=app.me
        )
        app.get_messages = AsyncMock(return_value=original)
        app.answer_callback_query = AsyncMock(return_value=True)
        with patch("summon.bot.modules.search.edit_text_card", new_callable=AsyncMock) as edit:
            await dispatch(app, callback_packet(buttons[0].callback_data))
        assert store.catalog.list.await_args.kwargs == {"search": "Aiko", "page": 2, "limit": 10}
        assert edit.await_args.args[1].title == "Character search"
        assert "Page 2" in edit.await_args.args[1].body
        assert app.answer_callback_query.await_count == 1
    finally:
        await app.dispatcher.stop()
