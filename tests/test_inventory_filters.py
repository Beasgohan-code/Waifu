import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from pymongo import ASCENDING, DESCENDING

from summon.api.app import create_app
from summon.config import Settings
from summon.repositories.catalog import CatalogRepository

TOKEN = "123456:TEST_TOKEN_DO_NOT_USE"
SECRET = "s" * 40


def sign_in(client, user_id):
    fields = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "first_name": "Mika"}),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    proof = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    client.headers["X-Summon-Launch"] = hashlib.sha256(proof.encode()).hexdigest()
    return client.post(
        "/api/auth/session",
        json={"init_data": urlencode({**fields, "hash": proof})},
        headers={"X-Summon-Request": "1"},
    )


def fake_sessions():
    return SimpleNamespace(
        create=AsyncMock(), is_active=AsyncMock(return_value=True), revoke=AsyncMock()
    )


def cursor(rows):
    result = MagicMock()
    result.sort.return_value = result
    result.skip.return_value = result
    result.limit.return_value = result
    result.to_list = AsyncMock(return_value=rows)
    return result


def character(identifier):
    return {
        "_id": identifier,
        "name": identifier.title(),
        "category": "Test",
        "rarity": "rare",
        "active": True,
    }


async def test_inventory_defaults_remain_owner_scoped_and_recently_ordered():
    rows = [{"user_id": 10, "character_id": "aiko", "quantity": 1, "favorite": False}]
    inventory_cursor = cursor(rows)
    chars_cursor = cursor([character("aiko")])
    inventory = SimpleNamespace(
        find=MagicMock(return_value=inventory_cursor), count_documents=AsyncMock(return_value=1)
    )
    chars = SimpleNamespace(find=MagicMock(return_value=chars_cursor))
    result = await CatalogRepository(
        SimpleNamespace(inventory=inventory, characters=chars)
    ).inventory(10)
    query = {"user_id": 10, "quantity": {"$gt": 0}}
    inventory.find.assert_called_once_with(query)
    inventory_cursor.sort.assert_called_once_with(
        [("acquired_at", DESCENDING), ("character_id", ASCENDING)]
    )
    inventory.count_documents.assert_awaited_once_with(query)
    assert [(entry["id"], entry["quantity"]) for entry in result["items"]] == [("aiko", 1)]


async def test_duplicate_and_favorite_filters_compose_with_stable_copy_sort_and_pagination():
    rows = [
        {"user_id": 10, "character_id": "ren", "quantity": 2, "favorite": True},
        {"user_id": 10, "character_id": "aiko", "quantity": 2, "favorite": True},
    ]
    inventory_cursor = cursor(rows)
    chars_cursor = cursor([character("aiko"), character("ren")])  # DB fetch order needn't match.
    inventory = SimpleNamespace(
        find=MagicMock(return_value=inventory_cursor), count_documents=AsyncMock(return_value=5)
    )
    chars = SimpleNamespace(find=MagicMock(return_value=chars_cursor))
    result = await CatalogRepository(
        SimpleNamespace(inventory=inventory, characters=chars)
    ).inventory(10, favorite=True, duplicates=True, sort="copies", page=2, limit=2)
    query = {"user_id": 10, "quantity": {"$gt": 1}, "favorite": True}
    inventory.find.assert_called_once_with(query)
    inventory_cursor.sort.assert_called_once_with(
        [("quantity", DESCENDING), ("character_id", ASCENDING)]
    )
    inventory_cursor.skip.assert_called_once_with(2)
    inventory_cursor.limit.assert_called_once_with(2)
    inventory.count_documents.assert_awaited_once_with(query)
    assert [entry["id"] for entry in result["items"]] == ["ren", "aiko"]
    assert result["total"] == 5


async def test_unknown_inventory_sort_is_rejected_before_a_database_query():
    inventory = SimpleNamespace(find=MagicMock())
    with pytest.raises(ValueError):
        await CatalogRepository(SimpleNamespace(inventory=inventory)).inventory(
            10, sort="not-a-sort"
        )
    inventory.find.assert_not_called()


def test_inventory_api_uses_authenticated_actor_and_validated_filter_options():
    repository = SimpleNamespace(
        inventory=AsyncMock(return_value={"items": [], "total": 0, "page": 2, "limit": 10})
    )
    store = SimpleNamespace(
        catalog=repository,
        users=SimpleNamespace(touch=AsyncMock()),
        auth=SimpleNamespace(consume=AsyncMock(return_value=True)),
        sessions=fake_sessions(),
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
    )
    with TestClient(
        create_app(
            settings=Settings(bot_token=TOKEN, session_secret=SECRET, _env_file=None), store=store
        )
    ) as client:
        assert client.get("/api/inventory?duplicates=true&sort=copies").status_code == 401
        assert sign_in(client, 123).status_code == 200
        response = client.get(
            "/api/inventory?user_id=999&favorite=true&duplicates=true&sort=copies&page=2&limit=10"
        )
        assert response.status_code == 200
        repository.inventory.assert_awaited_once_with(
            123, page=2, limit=10, favorite=True, duplicates=True, sort="copies"
        )
        assert client.get("/api/inventory?sort=quantity%3Bdelete").status_code == 422
        assert client.get("/api/inventory?duplicates=invalid").status_code == 422
        assert client.get("/api/inventory?page=26").status_code == 422


def test_read_only_preview_supports_real_duplicate_filter_and_sort():
    with TestClient(create_app(settings=Settings(preview_mode=True, _env_file=None))) as client:
        result = client.get("/api/inventory?duplicates=true&sort=copies").json()["data"]
        assert [(entry["id"], entry["quantity"]) for entry in result["items"]] == [("ren", 2)]
        assert result["total"] == 1
        assert (
            client.get("/api/inventory?favorite=true&duplicates=true").json()["data"]["total"] == 0
        )
        result = client.get("/api/inventory?sort=copies").json()["data"]
        assert [entry["id"] for entry in result["items"]] == ["ren", "aiko"]
        assert client.get("/api/inventory?sort=not-a-sort").status_code == 422
