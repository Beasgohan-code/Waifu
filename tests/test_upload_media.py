"""Upload input, bounded media and actor-bound signed-button validation."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from wzgram.enums import ChatType
from wzgram.types import Animation, Chat, Message, Photo, User, Video

from summon.bot.upload_media import (
    menu_token,
    parse_upload,
    read_motion,
    reply_media,
    valid_token,
    valid_upload_age,
)
from summon.services.artwork import InvalidArtwork
from summon.services.catbox import MAX_MEDIA_BYTES


def photo_message(**options):
    photo = Photo(
        file_id="photo-file-id",
        file_unique_id="photo-unique-id",
        width=16,
        height=16,
        file_size=100,
        date=datetime.now(UTC),
    )
    return Message(id=49, chat=Chat(id=42, type=ChatType.PRIVATE), photo=photo, **options)


def command_message(text='/upload "Toji Fushiguro" "Jujutsu Kaisen" 14', **options):
    return Message(
        id=50,
        chat=Chat(id=42, type=ChatType.PRIVATE),
        from_user=User(id=42),
        text=text,
        date=datetime.now(UTC),
        **options,
    )


def test_upload_parser_supports_quoted_pipe_and_reference_hyphens():
    for args in (
        '"Toji Fushiguro" "Jujutsu Kaisen" 14',
        "Toji Fushiguro | Jujutsu Kaisen | 14",
        "Toji-Fushiguro Jujutsu-Kaisen 14",
    ):
        parsed = parse_upload(args, "generated-id")
        assert (parsed.character_id, parsed.name, parsed.category, parsed.rarity) == (
            "generated-id",
            "Toji Fushiguro",
            "Jujutsu Kaisen",
            "amv-edition",
        )
    assert parse_upload("old-id | Toji | JJK | 2", "generated-id").character_id == "old-id"
    assert parse_upload('"Zero-Two" "Darling in the Franxx" 1', "x").name == "Zero-Two"


@pytest.mark.parametrize(
    "args", ["A B 0", "A B 19", "A B common", "Multi Word Anime 1", "'A B 1", "A | | 1"]
)
def test_upload_parser_rejects_ambiguous_or_invalid_details(args):
    with pytest.raises(ValueError):
        parse_upload(args, "generated-id")


def test_provider_token_is_bound_to_operator_bot_text_media_and_provider():
    key = b"menu-signing-key"
    command = command_message()
    photo = reply_media(photo_message())
    token = menu_token(key, 123, 42, command, photo, "catbox")
    assert len(token) == 16 and valid_token(token, token)
    assert not valid_token(token, menu_token(key, 123, 43, command, photo, "catbox"))
    assert not valid_token(token, menu_token(key, 124, 42, command, photo, "catbox"))
    assert not valid_token(token, menu_token(key, 123, 42, command, photo, "imgbb"))
    assert not valid_token(
        token, menu_token(key, 123, 42, command_message("/upload A B 1"), photo, "catbox")
    )
    changed_photo = photo_message()
    changed_photo.photo.file_unique_id = "different-media"
    assert not valid_token(
        token, menu_token(key, 123, 42, command, reply_media(changed_photo), "catbox")
    )


def test_upload_menu_expires_and_rejects_future_dates():
    now = datetime.now(UTC)
    assert valid_upload_age(command_message(), now=now)
    old = command_message()
    old.date = now - timedelta(minutes=16)
    future = command_message()
    future.date = now + timedelta(minutes=2)
    assert not valid_upload_age(old, now=now)
    assert not valid_upload_age(future, now=now)


@pytest.mark.parametrize(
    "flag",
    [{"has_protected_content": True}, {"has_media_spoiler": True}, {"ephemeral_message_id": 1}],
)
def test_upload_rejects_protected_spoiler_or_ephemeral_media(flag):
    with pytest.raises(InvalidArtwork):
        reply_media(photo_message(**flag))


async def test_video_is_bounded_and_sniffed_not_trusted_by_filename():
    content = b"\x00\x00\x00\x18ftypisom" + b"video"
    video = Video(
        file_id="video-id",
        file_unique_id="unique-video",
        width=16,
        height=16,
        codec="h264",
        duration=1,
        file_name="untrusted.exe",
        mime_type="video/mp4",
        file_size=len(content),
    )
    msg = Message(id=49, chat=Chat(id=42, type=ChatType.PRIVATE), video=video)
    media = reply_media(msg)

    async def stream(_message, *, limit):
        assert limit == 17
        yield content

    data, extension = await read_motion(
        SimpleNamespace(stream_media=stream), msg, media, asyncio.Semaphore(1)
    )
    assert data == content and extension == "mp4" and media.kind == "video"
    video.file_size = MAX_MEDIA_BYTES + 1
    with pytest.raises(InvalidArtwork):
        reply_media(msg)


async def test_animation_detects_gif_and_rejects_incomplete_download():
    content = b"GIF89a" + b"frame"
    animation = Animation(
        file_id="gif-id",
        file_unique_id="unique-gif",
        width=16,
        height=16,
        duration=1,
        mime_type="image/gif",
        file_size=len(content),
    )
    msg = Message(id=49, chat=Chat(id=42, type=ChatType.PRIVATE), animation=animation)
    media = reply_media(msg)

    async def stream(_message, *, limit):
        yield content

    assert (
        await read_motion(SimpleNamespace(stream_media=stream), msg, media, asyncio.Semaphore(1))
    )[1] == "gif"
    animation.file_size += 1
    media = reply_media(msg)
    with pytest.raises(InvalidArtwork):
        await read_motion(SimpleNamespace(stream_media=stream), msg, media, asyncio.Semaphore(1))
