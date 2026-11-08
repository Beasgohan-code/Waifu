"""Offline Catbox request and response validation; no public uploads occur."""

import gzip

import httpx
import pytest
from pydantic import SecretStr

from summon.services.catbox import MAX_MEDIA_BYTES, upload_catbox, valid_media_signature
from summon.services.imgbb import ImageHostError

JPEG = b"\xff\xd8\xff" + b"photo"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"video"
GIF = b"GIF89a" + b"frames"


@pytest.mark.parametrize(("content", "extension"), [(JPEG, "jpg"), (MP4, "mp4"), (GIF, "gif")])
async def test_catbox_posts_only_to_fixed_endpoint_and_returns_direct_url(content, extension):
    def handler(request):
        assert request.method == "POST"
        assert str(request.url) == "https://catbox.moe/user/api.php"
        assert b"fileupload" in request.content
        assert content in request.content
        assert b"userhash" in request.content
        return httpx.Response(200, content=f"https://files.catbox.moe/abc123.{extension}")

    url = await upload_catbox(
        content, extension, SecretStr("test-userhash"), transport=httpx.MockTransport(handler)
    )
    assert url == f"https://files.catbox.moe/abc123.{extension}"


async def test_catbox_anonymous_request_does_not_include_userhash():
    def handler(request):
        assert b"userhash" not in request.content
        return httpx.Response(200, content="https://files.catbox.moe/abc123.jpg")

    await upload_catbox(JPEG, "jpg", SecretStr(""), transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "url",
    [
        "http://files.catbox.moe/file.jpg",
        "https://evil.invalid/file.jpg",
        "https://files.catbox.moe.evil.invalid/file.jpg",
        "https://files.catbox.moe/file.jpg?secret=1",
        "https://files.catbox.moe/file.jpg#fragment",
        "https://files.catbox.moe:444/file.jpg",
        "https://files.catbox.moe/some/path/file.jpg",
        "https://user:pass@files.catbox.moe/file.jpg",
        "not a URL",
    ],
)
async def test_catbox_rejects_unexpected_or_capability_urls_without_echoing_them(url):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=url))
    with pytest.raises(ImageHostError) as error:
        await upload_catbox(JPEG, "jpg", SecretStr("test-key"), transport=transport)
    assert "test-key" not in str(error.value)
    assert url not in str(error.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://example.invalid/"}),
        httpx.Response(403, content=b"rejected"),
        httpx.Response(200, content=b"x" * 4097),
        httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(b"encoded")
        ),
        httpx.Response(200, content=b"\xff"),
    ],
)
async def test_catbox_rejects_redirects_errors_large_and_encoded_responses(response):
    transport = httpx.MockTransport(lambda _: response)
    with pytest.raises(ImageHostError):
        await upload_catbox(JPEG, "jpg", SecretStr(""), transport=transport)


@pytest.mark.parametrize(
    ("content", "extension"),
    [(b"", "jpg"), (b"not video", "mp4"), (JPEG, "exe"), (b"x" * (MAX_MEDIA_BYTES + 1), "jpg")],
)
async def test_catbox_rejects_bad_input_before_network(content, extension):
    def handler(_request):
        pytest.fail("Invalid media must not be sent")

    with pytest.raises(ImageHostError):
        await upload_catbox(
            content, extension, SecretStr(""), transport=httpx.MockTransport(handler)
        )


def test_media_signatures_cover_supported_types():
    assert valid_media_signature(GIF, "gif")
    assert valid_media_signature(MP4, "mp4")
    assert valid_media_signature(MP4, "mov")
    assert valid_media_signature(b"\x1a\x45\xdf\xa3rest", "webm")
    assert not valid_media_signature(GIF, "mp4")
