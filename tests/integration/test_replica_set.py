"""Opt-in real MongoDB race tests against a newly-created isolated database.

No database is contacted unless a separate test URI and explicit confirmation
are supplied. Cleanup is also opt-in and restricted to this test's random name.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pymongo import AsyncMongoClient

from summon.auth import TelegramUser
from summon.database import initialize_database
from summon.models import GroupSettings
from summon.repositories.auth import AuthRepository
from summon.repositories.collection import CollectionRepository
from summon.repositories.gifts import GiftRepository
from summon.repositories.sessions import SessionRepository
from summon.repositories.trades import TradeRepository
from summon.repositories.users import DailyAlreadyClaimed, UserRepository
from summon.seed import seed_characters
from summon.services.gifting import GiftConflict
from summon.services.trading import TradeConflict

pytestmark = pytest.mark.integration


@pytest.fixture
async def isolated_database():
    uri = os.environ.get("SUMMON_TEST_MONGO_URI")
    if not uri:
        pytest.skip("Set a dedicated test MongoDB URI to run replica-set integration tests")
    if os.environ.get("SUMMON_TEST_MONGO_CONFIRM") != "CREATE_ISOLATED_TEST_DB":
        pytest.fail("Explicit confirmation is required to create an isolated test database")
    name = f"summon_test_{uuid4().hex}"
    client = AsyncMongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=5000)
    try:
        hello = await client.admin.command("hello")
        if not (hello.get("setName") or hello.get("msg") == "isdbgrid"):
            pytest.fail("Integration tests require a replica set or sharded cluster")
        database = client[name]
        print(f"Isolated SUMMON integration database: {name}")
        await initialize_database(database)
        await seed_characters(database)
        yield client, database
    finally:
        # Never drop an existing or user-selected DB, and only with separate approval.
        if os.environ.get("SUMMON_TEST_MONGO_DROP_CONFIRM") == "DROP_ISOLATED_TEST_DB":
            await client.drop_database(name)
        await client.close()


async def test_concurrent_spawn_claim_awards_exactly_one_player(isolated_database):
    client, db = isolated_database
    now = datetime.now(UTC)
    settings = GroupSettings(spawn_enabled=True).model_dump()
    await db.groups.insert_one(
        {
            "_id": -100,
            "title": "Isolated test",
            "settings": settings,
            "message_count": settings["spawn_every"],
            "next_spawn_at": now,
            "active_spawn": None,
        }
    )
    repo = CollectionRepository(client, db)
    character = await db.characters.find_one({"_id": "aiko"})
    assert character is not None
    spawn = await repo.reserve_spawn(-100, character, now)
    assert spawn is not None
    results = await asyncio.gather(
        *(
            repo.claim_spawn(-100, {"id": user_id, "first_name": "Test"}, "aiko tsukino", now)
            for user_id in (101, 102)
        )
    )
    assert sum(result is not None for result in results) == 1
    inventory = await db.inventory.find({"character_id": "aiko"}).to_list(length=10)
    assert sum(row["quantity"] for row in inventory) == 1
    assert await db.reward_history.count_documents({"_id": f"catch:{spawn['id']}"}) == 1
    assert (await db.spawns.find_one({"_id": spawn["id"]}))["status"] == "claimed"


async def test_concurrent_daily_claim_cannot_duplicate_rewards(isolated_database):
    client, db = isolated_database
    repo = UserRepository(client, db)
    user = TelegramUser(id=123, first_name="Test")
    now = datetime.now(UTC)
    await repo.touch(user, now)
    results = await asyncio.gather(
        repo.daily(user, now), repo.daily(user, now), return_exceptions=True
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, DailyAlreadyClaimed) for result in results) == 1
    player = await db.users.find_one({"_id": user.id})
    assert player["points"] == 100
    assert await db.reward_history.count_documents({"user_id": user.id, "kind": "daily"}) == 1


async def test_concurrent_trade_acceptance_swaps_once(isolated_database):
    client, db = isolated_database
    now = datetime.now(UTC)
    await db.inventory.insert_many(
        [
            {"user_id": 11, "character_id": "aiko", "quantity": 1, "favorite": False},
            {"user_id": 22, "character_id": "ren", "quantity": 1, "favorite": False},
        ]
    )
    repo = TradeRepository(client, db)
    trade = await repo.create(11, 22, "aiko", "ren", now)
    results = await asyncio.gather(
        repo.confirm(trade["id"], 22, now),
        repo.confirm(trade["id"], 22, now),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, TradeConflict) for result in results) == 1
    rows = await db.inventory.find({"user_id": {"$in": [11, 22]}}).to_list(length=10)
    quantities = {(row["user_id"], row["character_id"]): row["quantity"] for row in rows}
    assert quantities == {(11, "aiko"): 0, (11, "ren"): 1, (22, "ren"): 0, (22, "aiko"): 1}
    assert await db.activity.count_documents({"_id": f"trade:{trade['id']}"}) == 1


async def test_concurrent_gift_acceptance_transfers_once_and_expiry_fails(isolated_database):
    client, db = isolated_database
    now = datetime.now(UTC)
    await db.inventory.insert_one({"user_id": 33, "character_id": "aiko", "quantity": 1})
    repo = GiftRepository(client, db)
    gift = await repo.create(33, 44, "aiko", now)
    with pytest.raises(GiftConflict):
        await repo.confirm(gift["id"], 44, now + timedelta(minutes=16))
    results = await asyncio.gather(
        repo.confirm(gift["id"], 44, now),
        repo.confirm(gift["id"], 44, now),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, GiftConflict) for result in results) == 1
    sender = await db.inventory.find_one({"user_id": 33, "character_id": "aiko"})
    receiver = await db.inventory.find_one({"user_id": 44, "character_id": "aiko"})
    assert sender["quantity"] == 0 and receiver["quantity"] == 1
    assert await db.activity.count_documents({"_id": f"gift:{gift['id']}"}) == 1


async def test_failed_spawn_delivery_clears_only_matching_reservation(isolated_database):
    client, db = isolated_database
    now = datetime.now(UTC)
    await db.groups.insert_one(
        {
            "_id": -100,
            "title": "Isolated test",
            "settings": GroupSettings().model_dump(),
            "message_count": 0,
            "active_spawn": {"id": "spawn-one", "expires_at": now + timedelta(minutes=5)},
        }
    )
    await db.spawns.insert_one(
        {"_id": "spawn-one", "group_id": -100, "status": "active", "created_at": now}
    )
    repo = CollectionRepository(client, db)
    await repo.mark_delivery_failed(-100, "old-spawn")
    assert (await db.groups.find_one({"_id": -100}))["active_spawn"]["id"] == "spawn-one"
    await repo.mark_delivery_failed(-100, "spawn-one")
    assert (await db.groups.find_one({"_id": -100}))["active_spawn"] is None
    assert (await db.spawns.find_one({"_id": "spawn-one"}))["status"] == "delivery_failed"
    await repo.mark_delivery_failed(-100, "spawn-one")  # Idempotent after cancellation.


async def test_failed_spawn_delivery_rolls_back_on_inconsistent_history(isolated_database):
    client, db = isolated_database
    now = datetime.now(UTC)
    await db.groups.insert_one(
        {
            "_id": -100,
            "title": "Isolated test",
            "settings": GroupSettings().model_dump(),
            "message_count": 0,
            "active_spawn": {"id": "spawn-one", "expires_at": now + timedelta(minutes=5)},
        }
    )
    # Deliberately inconsistent data in this newly created test DB only.
    await db.spawns.insert_one(
        {"_id": "spawn-one", "group_id": -100, "status": "claimed", "created_at": now}
    )
    repo = CollectionRepository(client, db)
    with pytest.raises(RuntimeError, match="history"):
        await repo.mark_delivery_failed(-100, "spawn-one")
    assert (await db.groups.find_one({"_id": -100}))["active_spawn"]["id"] == "spawn-one"
    assert (await db.spawns.find_one({"_id": "spawn-one"}))["status"] == "claimed"


async def test_session_expiry_and_revocation_across_repository_instances(isolated_database):
    _, db = isolated_database
    worker_a, worker_b = SessionRepository(db), SessionRepository(db)
    session_id = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    await worker_a.create(session_id, 123, now, 300)
    assert await worker_b.is_active(session_id, 123, now)
    assert not await worker_b.is_active(session_id, 456, now)
    assert not await worker_b.is_active(session_id, 123, now + timedelta(seconds=301))
    await worker_b.revoke(session_id, 123)
    assert not await worker_a.is_active(session_id, 123, now)


async def test_concurrent_launch_proof_consumption_is_unique_across_instances(isolated_database):
    _, db = isolated_database
    worker_a, worker_b = AuthRepository(db), AuthRepository(db)
    proof = "test-only-proof"
    now = datetime.now(UTC)
    results = await asyncio.gather(
        worker_a.consume(proof, now, 300), worker_b.consume(proof, now, 300)
    )
    assert sorted(results) == [False, True]
    assert await db.auth_nonces.count_documents({}) == 1


async def test_warning_revocation_race_rollback_and_legacy_counts(isolated_database):
    from types import SimpleNamespace

    from summon.repositories.groups import GroupRepository

    client, db = isolated_database
    repo = GroupRepository(client, db)
    now = datetime.now(UTC)
    first, second, foreign, rollback_id = ("f" * 32, "e" * 32, "d" * 32, "c" * 32)
    await db.warnings.insert_many(
        [
            {
                "_id": first,
                "group_id": -100,
                "user_id": 55,
                "actor_id": 7,
                "reason": "old without revoked field",
                "created_at": now,
            },
            {
                "_id": second,
                "group_id": -100,
                "user_id": 55,
                "actor_id": 8,
                "reason": "another",
                "created_at": now,
            },
            {
                "_id": foreign,
                "group_id": -101,
                "user_id": 55,
                "actor_id": 8,
                "reason": "other group",
                "created_at": now,
            },
            {
                "_id": rollback_id,
                "group_id": -100,
                "user_id": 56,
                "actor_id": 8,
                "reason": "rollback",
                "created_at": now,
            },
        ]
    )
    page = await repo.list_warnings(-100, 55, limit=1)
    assert page["active_count"] == 2 and [row["id"] for row in page["items"]] == [first]
    assert page["next_cursor"] == first
    with pytest.raises(LookupError):
        await repo.revoke_warning(-100, 42, 55, foreign, "wrong group", now)
    results = await asyncio.gather(
        repo.revoke_warning(-100, 42, 55, first, "one", now),
        repo.revoke_warning(-100, 43, 55, first, "two", now),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, LookupError) for result in results) == 1
    assert (
        await db.activity.count_documents({"action": "warning_revoked", "warning_id": first}) == 1
    )
    revoked = await db.warnings.find_one({"_id": first})
    assert revoked["revoked_by"] in {42, 43} and revoked["revoked_at"] is not None
    # A revoked page anchor remains usable, without including it as active.
    older = await repo.list_warnings(-100, 55, before=first, limit=1)
    assert older["active_count"] == 1 and [row["id"] for row in older["items"]] == [second]
    issued = await repo.record_moderation(-100, 42, 55, "warn", "new", now)
    assert issued["warning_count"] == 2

    class FailingActivity:
        async def insert_one(self, *args, **kwargs):
            await db.activity.insert_one(*args, **kwargs)
            raise RuntimeError("injected audit failure")

    failing = GroupRepository(
        client, SimpleNamespace(warnings=db.warnings, activity=FailingActivity())
    )
    with pytest.raises(RuntimeError, match="injected"):
        await failing.revoke_warning(-100, 42, 56, rollback_id, "rollback", now)
    assert "revoked_at" not in (await db.warnings.find_one({"_id": rollback_id}))
    assert await db.activity.count_documents({"warning_id": rollback_id}) == 0


async def test_note_concurrent_edits_and_audit_rollback(isolated_database):
    from types import SimpleNamespace

    from summon.group_notes import NoteConflict
    from summon.repositories.group_notes import GroupNoteRepository

    client, db = isolated_database
    now = datetime.now(UTC)
    await db.groups.insert_many(
        [
            {
                "_id": -100,
                "title": "Notes group",
                "settings": GroupSettings().model_dump(),
                "message_count": 0,
            },
            {
                "_id": -200,
                "title": "Other group",
                "settings": GroupSettings().model_dump(),
                "message_count": 0,
            },
        ]
    )
    repo = GroupNoteRepository(client, db)
    original = await repo.save(-100, 42, "guide", "Original", None, now)
    results = await asyncio.gather(
        repo.save(-100, 42, "guide", "First edit", original["revision"], now),
        repo.save(-100, 43, "guide", "Second edit", original["revision"], now),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, NoteConflict) for result in results) == 1
    winner = next(result for result in results if isinstance(result, dict))
    assert await db.activity.count_documents({"group_id": -100, "kind": "note_changed"}) == 2
    with pytest.raises(NoteConflict):
        await repo.delete(-100, 42, "guide", original["revision"], now)
    with pytest.raises(NoteConflict):
        await repo.delete(-200, 42, "guide", winner["revision"], now)
    assert (await repo.get(-100, "guide"))["revision"] == winner["revision"]

    class FailingActivity:
        async def insert_one(self, *args, **kwargs):
            await db.activity.insert_one(*args, **kwargs)
            raise RuntimeError("injected audit failure")

    failing = GroupNoteRepository(
        client, SimpleNamespace(groups=db.groups, activity=FailingActivity())
    )
    with pytest.raises(RuntimeError, match="injected"):
        await failing.save(-100, 42, "guide", "Should roll back", winner["revision"], now)
    assert (await repo.get(-100, "guide"))["revision"] == winner["revision"]
    assert await db.activity.count_documents({"group_id": -100, "kind": "note_changed"}) == 2
    with pytest.raises(RuntimeError, match="injected"):
        await failing.delete(-100, 42, "guide", winner["revision"], now)
    assert (await repo.get(-100, "guide"))["revision"] == winner["revision"]
    assert await db.activity.count_documents({"group_id": -100, "kind": "note_changed"}) == 2


async def test_note_cap_is_atomic_under_concurrent_creates(isolated_database):
    from summon.group_notes import NoteLimitError
    from summon.repositories.group_notes import GroupNoteRepository

    client, db = isolated_database
    now = datetime.now(UTC)
    note = {
        "content": "Example",
        "revision": "a" * 32,
        "created_at": now,
        "created_by": 42,
        "updated_at": now,
        "updated_by": 42,
    }
    await db.groups.insert_one(
        {
            "_id": -100,
            "title": "Notes group",
            "settings": GroupSettings().model_dump(),
            "message_count": 0,
            "saved_notes_v1": {f"n{i}": note for i in range(29)},
        }
    )
    repo = GroupNoteRepository(client, db)
    results = await asyncio.gather(
        repo.save(-100, 42, "new-a", "First", None, now),
        repo.save(-100, 43, "new-b", "Second", None, now),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, NoteLimitError) for result in results) == 1
    assert len(await repo.list(-100)) == 30
    assert await db.activity.count_documents({"group_id": -100, "kind": "note_changed"}) == 1
