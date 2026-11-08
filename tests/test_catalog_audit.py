"""Catalog mutations record operator attribution in the same transaction."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from summon.models import CharacterInput
from summon.repositories.catalog import CatalogRepository


async def test_catalog_write_and_operator_audit_use_one_transaction(monkeypatch):
    async def fake_transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.catalog.transaction", fake_transaction)
    db = SimpleNamespace(
        characters=SimpleNamespace(update_one=AsyncMock(), find_one=AsyncMock(return_value=None)),
        activity=SimpleNamespace(insert_one=AsyncMock()),
        catalog_counters=SimpleNamespace(
            find_one=AsyncMock(return_value={"_id": "characters"}),
            find_one_and_update=AsyncMock(return_value={"last_number": 1}),
        ),
    )
    repo = CatalogRepository(db, client=object())
    data = CharacterInput(
        id="aiko", name="Aiko", category="Sky", rarity="rare", image_url="/art/aiko.webp"
    )
    saved = await repo.upsert_character(data, actor_id=42)
    assert saved["id"] == "aiko"
    assert db.characters.update_one.await_args.kwargs["session"] == "session"
    audit = db.activity.insert_one.await_args
    assert audit.kwargs["session"] == "session"
    assert audit.args[0]["user_id"] == 42
    assert audit.args[0]["kind"] == "character_saved"
    assert audit.args[0]["character_id"] == "aiko"


async def test_catalog_write_fails_closed_without_a_transaction_client():
    repo = CatalogRepository(SimpleNamespace(characters=SimpleNamespace(update_one=AsyncMock())))
    data = CharacterInput(
        id="aiko", name="Aiko", category="Sky", rarity="rare", image_url="/art/aiko.webp"
    )
    with pytest.raises(RuntimeError):
        await repo.upsert_character(data, actor_id=42)
    repo.db.characters.update_one.assert_not_awaited()


async def test_unrelated_admin_update_preserves_existing_video_file_id(monkeypatch):
    async def fake_transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.catalog.transaction", fake_transaction)
    current = {
        "_id": "clip",
        "number": 5,
        "image_url": "https://files.catbox.moe/clip.mp4",
        "media_type": "video",
        "telegram_file_id": "telegram-file-id",
    }
    db = SimpleNamespace(
        characters=SimpleNamespace(
            find_one=AsyncMock(return_value=current), update_one=AsyncMock()
        ),
        activity=SimpleNamespace(insert_one=AsyncMock()),
        catalog_counters=SimpleNamespace(find_one=AsyncMock(return_value={"_id": "characters"})),
    )
    repo = CatalogRepository(db, client=object())
    data = CharacterInput(
        id="clip",
        name="New name",
        category="Sky",
        rarity="amv-edition",
        image_url=current["image_url"],
    )
    saved = await repo.upsert_character(data, actor_id=42)
    changes = db.characters.update_one.await_args.args[1]["$set"]
    assert "telegram_file_id" not in changes and "media_type" not in changes
    assert saved["media_type"] == "video" and saved["number"] == 5
    assert "telegram_file_id" not in saved


async def test_replacing_admin_url_drops_stale_file_id(monkeypatch):
    async def fake_transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.catalog.transaction", fake_transaction)
    db = SimpleNamespace(
        characters=SimpleNamespace(
            find_one=AsyncMock(
                return_value={
                    "_id": "clip",
                    "number": 5,
                    "image_url": "https://files.catbox.moe/old.mp4",
                    "media_type": "video",
                    "telegram_file_id": "old-file-id",
                }
            ),
            update_one=AsyncMock(),
        ),
        activity=SimpleNamespace(insert_one=AsyncMock()),
        catalog_counters=SimpleNamespace(find_one=AsyncMock(return_value={"_id": "characters"})),
    )
    data = CharacterInput(
        id="clip",
        name="Name",
        category="Sky",
        rarity="rare",
        image_url="https://files.catbox.moe/new.jpg",
    )
    await CatalogRepository(db, client=object()).upsert_character(data, actor_id=42)
    changes = db.characters.update_one.await_args.args[1]["$set"]
    assert changes["telegram_file_id"] is None and changes["media_type"] == "photo"


async def test_photo_update_saves_new_file_id_atomically_and_rejects_unrelated_ids(monkeypatch):
    from summon.models import CharacterChanges

    async def fake_transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.repositories.catalog.transaction", fake_transaction)
    db = SimpleNamespace(
        characters=SimpleNamespace(
            update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1))
        ),
        activity=SimpleNamespace(insert_one=AsyncMock()),
    )
    repo = CatalogRepository(db, client=object())
    with pytest.raises(ValueError, match="requires a new artwork URL"):
        await repo.update_character(
            "a", CharacterChanges(name="New"), actor_id=42, event_id="bad", telegram_file_id="id"
        )
    db.activity.insert_one.assert_not_awaited()
    await repo.update_character(
        "a",
        CharacterChanges(image_url="https://i.ibb.co/example/photo.jpg"),
        actor_id=42,
        event_id="good",
        telegram_file_id="new-photo-id",
    )
    update = db.characters.update_one.await_args.args[1]["$set"]
    assert update["media_type"] == "photo"
    assert update["telegram_file_id"] == "new-photo-id"
    assert db.activity.insert_one.await_args.kwargs["session"] == "session"
