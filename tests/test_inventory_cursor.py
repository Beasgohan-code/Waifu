from __future__ import annotations

import copy
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson.timestamp import Timestamp
from fastapi.testclient import TestClient

from summon.api.app import create_app
from summon.config import Settings
from summon.repositories.inventory_paging import (
    CursorCodec,
    CursorDataError,
    CursorExpired,
    CursorInvalid,
    CursorState,
    CursorUnavailable,
    SnapshotInventory,
    seek_after,
)

SECRET = "test-only-session-secret-that-is-long-enough"


def state(**overrides):
    return CursorState(
        user_id=42,
        favorite=False,
        duplicates=False,
        sort="recent",
        limit=24,
        snapshot_seconds=123,
        snapshot_increment=1,
        last_id="item-0001",
        last_value="2026-01-01T00:00:00+00:00",
        page=2,
        total=650,
        consumed=24,
        started_at=int(time.time()),
        preview=False,
        **overrides,
    )


def test_cursor_is_signed_and_bound_to_actor_filters_sort_and_limit():
    codec = CursorCodec(SECRET)
    token = codec.encode(state())
    options = dict(
        user_id=42, favorite=False, duplicates=False, sort="recent", limit=24, preview=False
    )
    assert codec.decode(token, **options).last_id == "item-0001"
    for key, altered in (
        ("user_id", 43),
        ("favorite", True),
        ("duplicates", True),
        ("sort", "copies"),
        ("limit", 50),
        ("preview", True),
    ):
        with pytest.raises(CursorInvalid):
            codec.decode(token, **{**options, key: altered})
    index = token.rfind(".") + 2
    replacement = "A" if token[index] != "A" else "B"
    with pytest.raises(CursorInvalid):
        codec.decode(token[:index] + replacement + token[index + 1 :], **options)


def test_cursor_expiry_is_from_the_first_page_not_each_new_token():
    old = state().model_copy(update={"started_at": int(time.time()) - 241})
    codec = CursorCodec(SECRET)
    with pytest.raises(CursorExpired):
        codec.decode(
            codec.encode(old),
            user_id=42,
            favorite=False,
            duplicates=False,
            sort="recent",
            limit=24,
            preview=False,
        )


def test_seek_predicates_cover_equal_values_and_legacy_missing_dates():
    when = datetime(2026, 1, 1, tzinfo=UTC)
    assert seek_after("recent", when.isoformat(), "aiko") == {
        "$or": [
            {"acquired_at": {"$lt": when}},
            {"acquired_at": when, "character_id": {"$gt": "aiko"}},
            {"acquired_at": None},
        ]
    }
    assert seek_after("recent", None, "aiko") == {
        "acquired_at": None,
        "character_id": {"$gt": "aiko"},
    }
    assert seek_after("copies", 3, "aiko") == {
        "$or": [
            {"quantity": {"$lt": 3}},
            {"quantity": 3, "character_id": {"$gt": "aiko"}},
        ]
    }
    with pytest.raises(CursorInvalid):
        seek_after("recent", "not-a-date", "aiko")


def matches(row, expression):
    if "$and" in expression:
        return all(matches(row, item) for item in expression["$and"])
    if "$or" in expression:
        return any(matches(row, item) for item in expression["$or"])
    for field, expected in expression.items():
        value = row.get(field)
        if isinstance(expected, dict):
            for op, bound in expected.items():
                try:
                    if op == "$gt" and not (value is not None and value > bound):
                        return False
                    if op == "$lt" and not (value is not None and value < bound):
                        return False
                except TypeError:  # MongoDB comparisons generally type-bracket query operands.
                    return False
        elif value != expected:
            return False
    return True


class FakeSnapshotReader:
    def __init__(self, rows):
        self.live = rows
        self.snapshot = []
        self.stamp = Timestamp(123, 1)
        self.commands = []

    async def aggregate(self, _session, pipeline, at, batch_size):
        self.commands.append((copy.deepcopy(pipeline), at, batch_size))
        if at is None:
            self.snapshot = copy.deepcopy(self.live)
        else:
            assert at == self.stamp
        rows = [copy.deepcopy(row) for row in self.snapshot if matches(row, pipeline[0]["$match"])]
        if "$group" in pipeline[-1]:
            selected = [row for row in rows if matches(row, self.base)]
            invalid_time = sum(
                row.get("acquired_at") is not None and not isinstance(row["acquired_at"], datetime)
                for row in selected
            )
            invalid_quantity = sum(
                type(row.get("quantity")) is not int or row["quantity"] < 0 for row in rows
            )
            invalid_id = sum(
                not isinstance(row.get("character_id"), str)
                or not row["character_id"]
                or len(row["character_id"]) > 512
                for row in selected
            )
            return (
                [
                    {
                        "total": sum(type(row.get("quantity")) is int for row in selected),
                        "invalid_time": invalid_time,
                        "invalid_quantity": invalid_quantity,
                        "invalid_id": invalid_id,
                    }
                ]
                if rows
                else []
            ), self.stamp
        match = pipeline[0]["$match"]
        self.base = match["$and"][0] if "$and" in match else match
        ordering = pipeline[1]["$sort"]
        field = next(iter(ordering))
        rows.sort(key=lambda item: item["character_id"])
        minimum = datetime.min.replace(tzinfo=UTC) if field == "acquired_at" else 0
        rows.sort(key=lambda item: item.get(field) or minimum, reverse=True)
        rows = rows[: pipeline[2]["$limit"]]
        for row in rows:
            row["character"] = [
                {
                    "_id": row["character_id"],
                    "name": row["character_id"],
                    "category": "Test",
                    "rarity": "rare",
                    "active": True,
                }
            ]
        return rows, self.stamp


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def client():
    return SimpleNamespace(
        admin=SimpleNamespace(command=AsyncMock(return_value={"maxWireVersion": 17})),
        start_session=lambda **_kwargs: FakeSession(),
    )


@pytest.mark.parametrize("sort", ["recent", "copies"])
async def test_over_600_snapshot_pages_ignore_new_and_mutated_entries(sort):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {
            "user_id": 42,
            "character_id": f"item-{index:04d}",
            "quantity": 1 + index % 4,
            "favorite": index % 2 == 0,
            **({"acquired_at": now + timedelta(seconds=index % 5)} if index % 7 else {}),
        }
        for index in range(650)
    ]
    engine = FakeSnapshotReader(rows)
    pager = SnapshotInventory(object(), client())
    pager._aggregate = engine.aggregate
    page = await pager.page(42, secret=SECRET, limit=24, sort=sort)
    assert page["total"] == 650 and page["page"] == 1
    observed = list(page["items"])
    start_cursor = page["page_cursor"]
    engine.live.append({"user_id": 42, "character_id": "new", "quantity": 99, "acquired_at": now})
    engine.live[-10]["quantity"] = 100
    engine.live[-11]["favorite"] = not engine.live[-11]["favorite"]
    replay = await pager.page(42, secret=SECRET, limit=24, sort=sort, cursor=start_cursor)
    assert replay["items"] == observed and replay["total"] == 650
    while page["next_cursor"]:
        page = await pager.page(42, secret=SECRET, limit=24, sort=sort, cursor=page["next_cursor"])
        observed.extend(page["items"])
    assert page["page"] == 28
    assert len(observed) == 650
    assert len({item["id"] for item in observed}) == 650
    assert {item["id"] for item in observed} == {row["character_id"] for row in engine.snapshot}
    assert "new" not in {item["id"] for item in observed}
    assert all(at == engine.stamp for _, at, _ in engine.commands[1:])


def test_preview_cursor_pages_are_bound_and_legacy_paging_still_works():
    with TestClient(create_app(settings=Settings(preview_mode=True, _env_file=None))) as client:
        first = client.get("/api/inventory/pages?limit=1").json()["data"]
        assert first["total"] == 2 and first["page"] == 1
        token = first["next_cursor"]
        assert token
        second = client.get("/api/inventory/pages", params={"limit": 1, "cursor": token}).json()[
            "data"
        ]
        assert second["page"] == 2 and second["next_cursor"] is None
        assert first["items"][0]["id"] != second["items"][0]["id"]
        assert (
            client.get(
                "/api/inventory/pages", params={"limit": 1, "cursor": token, "favorite": True}
            ).status_code
            == 400
        )
        assert client.get("/api/inventory?page=1&limit=1").status_code == 200
        assert client.get("/api/inventory/pages?cursor=broken").status_code == 400


async def test_mongodb_44_has_explicit_compatibility_failure():
    db_client = client()
    db_client.admin.command.return_value = {"maxWireVersion": 9}
    pager = SnapshotInventory(object(), db_client)
    with pytest.raises(CursorUnavailable, match="5.0"):
        await pager.page(42, secret=SECRET)


@pytest.mark.parametrize(
    "row,sort",
    [
        ({"character_id": "bad", "quantity": 1, "acquired_at": "not a date"}, "recent"),
        ({"character_id": "bad", "quantity": "2"}, "copies"),
        ({"character_id": "bad", "quantity": 2.5}, "copies"),
        ({"character_id": "", "quantity": 1}, "recent"),
    ],
)
async def test_corrupt_sort_data_fails_closed_instead_of_skipping(row, sort):
    engine = FakeSnapshotReader([{"user_id": 42, **row}])
    pager = SnapshotInventory(object(), client())
    pager._aggregate = engine.aggregate
    with pytest.raises(CursorDataError):
        await pager.page(42, secret=SECRET, limit=1, sort=sort)
