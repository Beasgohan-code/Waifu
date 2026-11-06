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
        characters=SimpleNamespace(update_one=AsyncMock()),
        activity=SimpleNamespace(insert_one=AsyncMock()),
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
