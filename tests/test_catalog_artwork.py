"""Catalog artwork limits, transaction boundaries and public delivery."""

import hashlib
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pymongo.errors import DuplicateKeyError

from summon.api.app import create_app
from summon.config import Settings
from summon.models import CharacterChanges, CharacterInput
from summon.repositories.catalog import CatalogConflict, CatalogNotFound, CatalogRepository
from summon.services.artwork import MAX_ARTWORK_BYTES, Artwork, InvalidArtwork, normalize_photo


def photo_bytes(*, size=(16, 16), file_format="JPEG"):
    image = Image.new("RGB", size, color="purple")
    exif = Image.Exif()
    exif[0x010E] = "FAKE_PRIVATE_METADATA"
    output = BytesIO()
    image.save(output, format=file_format, exif=exif)
    return output.getvalue()


async def test_photo_is_transcoded_bounded_and_strips_private_metadata():
    original = photo_bytes()
    assert b"FAKE_PRIVATE_METADATA" in original
    processed = await normalize_photo(original)
    assert processed.path == f"/api/artwork/{hashlib.sha256(processed.content).hexdigest()}"
    assert len(processed.content) <= MAX_ARTWORK_BYTES
    assert b"FAKE_PRIVATE_METADATA" not in processed.content
    with Image.open(BytesIO(processed.content)) as image:
        assert image.format == "JPEG"
        assert not image.getexif()
        assert "icc_profile" not in image.info


@pytest.mark.parametrize(
    "content",
    [b"", b"x" * (MAX_ARTWORK_BYTES + 1), b"not an image", photo_bytes(file_format="PNG")],
)
async def test_photo_rejects_oversized_corrupt_and_non_jpeg_inputs(content):
    with pytest.raises(InvalidArtwork):
        await normalize_photo(content)


async def test_photo_rejects_excessive_dimensions_before_decoding():
    with pytest.raises(InvalidArtwork):
        await normalize_photo(photo_bytes(size=(9000, 1)))


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/a.jpg",
        "https://localhost/a.jpg",
        "https://foo.local/a.jpg",
        "https://127.0.0.1/a.jpg",
        "https://[::1]/a.jpg",
        "https://user:secret@example.com/a.jpg",
        "https://example.com/a.jpg#fragment",
        "https://example.com/a.jpg\n.evil",
        "https://example.com:99999/a.jpg",
    ],
)
def test_catalog_rejects_unsafe_urls_without_echoing_them(url):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CharacterInput(id="a", name="A", category="S", rarity="rare", image_url=url)


def test_catalog_accepts_public_https_and_content_addressed_paths():
    data = dict(id="a", name="A", category="S", rarity="rare")
    assert CharacterInput(**data, image_url="https://cdn.example/a.jpg").image_url.startswith(
        "https:"
    )
    assert CharacterInput(**data, image_url=f"/api/artwork/{'a' * 64}").image_url.endswith("a" * 64)
    with pytest.raises(ValueError):
        CharacterInput(**data, image_url="/api/artwork/../../secret")


def fake_db():
    return SimpleNamespace(
        activity=SimpleNamespace(insert_one=AsyncMock(), find_one=AsyncMock(return_value=None)),
        characters=SimpleNamespace(
            insert_one=AsyncMock(), update_one=AsyncMock(), find_one=AsyncMock(return_value=None)
        ),
        character_artworks=SimpleNamespace(update_one=AsyncMock(), find_one=AsyncMock()),
        inventory=SimpleNamespace(update_one=AsyncMock(), delete_one=AsyncMock()),
    )


async def test_create_commits_catalog_art_and_actor_audit_in_one_transaction(monkeypatch):
    async def transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.catalog.transaction", transaction)
    db = fake_db()
    artwork = await normalize_photo(photo_bytes())
    data = CharacterInput(
        id="aiko", name="Aiko", category="Sky", rarity="special-edition", image_url=artwork.path
    )
    result = await CatalogRepository(db, client=object()).create_character(
        data, actor_id=42, event_id="event-1", artwork=artwork
    )
    assert result["id"] == "aiko"
    assert db.characters.insert_one.await_args.kwargs["session"] == "session"
    assert db.activity.insert_one.await_args.kwargs["session"] == "session"
    assert db.character_artworks.update_one.await_args.kwargs["session"] == "session"
    assert db.activity.insert_one.await_args.args[0]["user_id"] == 42
    assert db.activity.insert_one.await_args.args[0]["_id"] == "event-1"
    db.inventory.update_one.assert_not_awaited()
    db.inventory.delete_one.assert_not_awaited()


async def test_replayed_audit_id_fails_before_editing_or_touching_ownership(monkeypatch):
    async def transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.catalog.transaction", transaction)
    db = fake_db()
    db.activity.insert_one.side_effect = DuplicateKeyError("already processed")
    with pytest.raises(CatalogConflict):
        await CatalogRepository(db, client=object()).update_character(
            "aiko", CharacterChanges(name="New"), actor_id=42, event_id="same-message"
        )
    db.characters.update_one.assert_not_awaited()
    db.inventory.update_one.assert_not_awaited()


async def test_update_missing_id_aborts_and_does_not_store_artwork(monkeypatch):
    async def transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.catalog.transaction", transaction)
    db = fake_db()
    db.characters.update_one.return_value = SimpleNamespace(matched_count=0)
    artwork = await normalize_photo(photo_bytes())
    with pytest.raises(CatalogNotFound):
        await CatalogRepository(db, client=object()).update_character(
            "missing",
            CharacterChanges(image_url=artwork.path),
            actor_id=42,
            event_id="event-2",
            artwork=artwork,
        )
    db.character_artworks.update_one.assert_not_awaited()
    db.inventory.update_one.assert_not_awaited()


async def test_invalid_processed_artwork_is_rejected_before_database_write():
    db = fake_db()
    data = CharacterInput(
        id="a", name="A", category="S", rarity="rare", image_url=f"/api/artwork/{'a' * 64}"
    )
    with pytest.raises(ValueError):
        await CatalogRepository(db, client=object()).create_character(
            data, actor_id=42, event_id="id", artwork=Artwork("a" * 64, b"bad")
        )
    db.activity.insert_one.assert_not_awaited()


async def test_artwork_repository_refuses_tampered_content():
    db = fake_db()
    db.character_artworks.find_one.return_value = {"content": photo_bytes()}
    assert await CatalogRepository(db).artwork("a" * 64) is None
    original = photo_bytes()
    digest = hashlib.sha256(original).hexdigest()
    assert await CatalogRepository(db).artwork(digest) == original


def test_public_artwork_endpoint_is_bounded_cacheable_and_rate_limited():
    content = photo_bytes()
    digest = hashlib.sha256(content).hexdigest()
    catalog = SimpleNamespace(artwork=AsyncMock(return_value=content))
    limiter = SimpleNamespace(allow=AsyncMock(return_value=True))
    store = SimpleNamespace(catalog=catalog, limiter=limiter)
    with TestClient(create_app(settings=Settings(_env_file=None), store=store)) as client:
        response = client.get(f"/api/artwork/{digest}")
        assert response.status_code == 200 and response.content == content
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "public, max-age=3600, immutable"
        assert response.headers["x-content-type-options"] == "nosniff"
        catalog.artwork.reset_mock()
        limiter.allow.return_value = False
        blocked = client.get(f"/api/artwork/{digest}")
        assert blocked.status_code == 429
        assert blocked.headers["cache-control"] == "no-store"
        catalog.artwork.assert_not_awaited()
        assert client.get("/api/artwork/not-a-hash").status_code == 422
        limiter.allow.return_value = True
        catalog.artwork.return_value = None
        assert client.get(f"/api/artwork/{'a' * 64}").status_code == 404
        assert limiter.allow.await_count == 3  # Hits, blocked requests, and valid-hash misses.


async def test_imgbb_upload_uses_fixed_https_post_and_keeps_key_out_of_url(caplog):
    import httpx
    from pydantic import SecretStr

    from summon.services.imgbb import upload_imgbb

    image = await normalize_photo(photo_bytes())
    requests = []

    async def receive(request):
        requests.append(request)
        body = await request.aread()
        assert request.method == "POST"
        assert str(request.url) == "https://api.imgbb.com/1/upload"
        assert b"TEST_HOST_KEY_DO_NOT_LOG" in body
        assert image.content in body
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "url": "https://i.ibb.co/abc/image.jpg",
                    "delete_url": "PRIVATE_DELETE_URL",
                },
            },
        )

    result = await upload_imgbb(
        image, SecretStr("TEST_HOST_KEY_DO_NOT_LOG"), transport=httpx.MockTransport(receive)
    )
    assert result == "https://i.ibb.co/abc/image.jpg"
    assert len(requests) == 1
    assert "TEST_HOST_KEY_DO_NOT_LOG" not in caplog.text
    assert "PRIVATE_DELETE_URL" not in caplog.text


@pytest.mark.parametrize(
    "response",
    [
        {"status_code": 301, "headers": {"location": "https://other.example"}},
        {"status_code": 403, "text": "PRIVATE_PROVIDER_DETAIL"},
        {"status_code": 200, "text": "x" * (32 * 1024 + 1)},
        {"status_code": 200, "json": {"success": False}},
        {
            "status_code": 200,
            "json": {"success": True, "data": {"url": "http://127.0.0.1/private"}},
        },
        {
            "status_code": 200,
            "json": {"success": True, "data": {"url": "https://elsewhere.example/image"}},
        },
        {
            "status_code": 200,
            "json": {"success": True, "data": {"url": "https://i.ibb.co/a?token=secret"}},
        },
    ],
)
async def test_imgbb_untrusted_results_are_bounded_redacted_and_never_retried(response, caplog):
    import httpx
    from pydantic import SecretStr

    from summon.services.imgbb import ImageHostError, upload_imgbb

    image = await normalize_photo(photo_bytes())
    calls = []

    async def receive(request):
        calls.append(request)
        return httpx.Response(**response)

    with pytest.raises(ImageHostError) as error:
        await upload_imgbb(
            image, SecretStr("PRIVATE_HOST_KEY"), transport=httpx.MockTransport(receive)
        )
    assert len(calls) == 1
    assert "PRIVATE_HOST_KEY" not in str(error.value) + caplog.text
    assert "PRIVATE_PROVIDER_DETAIL" not in str(error.value) + caplog.text
    assert "token=secret" not in str(error.value) + caplog.text


async def test_imgbb_transport_timeout_is_uncertain_and_not_retried():
    import httpx
    from pydantic import SecretStr

    from summon.services.imgbb import ImageHostError, upload_imgbb

    image = await normalize_photo(photo_bytes())
    calls = []

    async def receive(request):
        calls.append(request)
        raise httpx.ReadTimeout("PRIVATE_NETWORK_DETAIL", request=request)

    with pytest.raises(ImageHostError) as error:
        await upload_imgbb(
            image, SecretStr("PRIVATE_HOST_KEY"), transport=httpx.MockTransport(receive)
        )
    assert len(calls) == 1
    assert "PRIVATE_NETWORK_DETAIL" not in str(error.value)


async def test_imgbb_preflight_queries_only_ids_and_does_not_mutate_storage():
    db = fake_db()
    repository = CatalogRepository(db)
    assert not await repository.catalog_event_exists("synthetic-event")
    assert not await repository.character_exists("aiko")
    db.activity.find_one.assert_awaited_once_with({"_id": "synthetic-event"}, {"_id": 1})
    db.characters.find_one.assert_awaited_once_with({"_id": "aiko"}, {"_id": 1})
    db.activity.insert_one.assert_not_awaited()
    db.characters.update_one.assert_not_awaited()


async def test_imgbb_rejects_encoded_response_before_reading_or_decompressing():
    import httpx
    from pydantic import SecretStr

    from summon.services.imgbb import ImageHostError, upload_imgbb

    image = await normalize_photo(photo_bytes())
    stream_read = False

    class EncodedResponse(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal stream_read
            stream_read = True
            yield b"untrusted compressed bytes"

    async def receive(_request):
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=EncodedResponse())

    with pytest.raises(ImageHostError, match="encoded response"):
        await upload_imgbb(image, SecretStr("PRIVATE_KEY"), transport=httpx.MockTransport(receive))
    assert not stream_read


async def test_imgbb_stream_errors_are_sanitized_and_not_retried():
    import httpx
    from pydantic import SecretStr

    from summon.services.imgbb import ImageHostError, upload_imgbb

    image = await normalize_photo(photo_bytes())
    calls = 0

    class BrokenResponse(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.StreamConsumed("PRIVATE_STREAM_DETAIL")
            yield b"never reached"

    async def receive(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=BrokenResponse())

    with pytest.raises(ImageHostError) as error:
        await upload_imgbb(image, SecretStr("PRIVATE_KEY"), transport=httpx.MockTransport(receive))
    assert calls == 1
    assert "PRIVATE_STREAM_DETAIL" not in str(error.value)
