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
from summon.repositories.users import DailyAlreadyClaimed, DailyStatusUnavailable, UserRepository
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


async def test_daily_utc_boundary_race_and_out_of_order_request(isolated_database):
    client, db = isolated_database
    repo = UserRepository(client, db)
    user = TelegramUser(id=124, first_name="Test")
    before = datetime(2026, 1, 1, 23, 59, 59, tzinfo=UTC)
    after = before + timedelta(seconds=1)
    first = await repo.daily(user, before)
    results = await asyncio.gather(
        *(repo.daily(user, after) for _ in range(4)), return_exceptions=True
    )
    rewards = [result for result in results if isinstance(result, dict)]
    assert first["points"] == 100 and first["streak"] == 1
    assert len(rewards) == 1
    assert rewards[0]["points"] == 110 and rewards[0]["streak"] == 2
    assert sum(isinstance(result, DailyAlreadyClaimed) for result in results) == 3
    with pytest.raises(DailyStatusUnavailable):
        await repo.daily(user, before)
    player = await db.users.find_one({"_id": user.id})
    assert player["points"] == 210 and player["xp"] == 40
    assert player["last_daily"] == "2026-01-02"
    assert await db.reward_history.count_documents({"user_id": user.id, "kind": "daily"}) == 2


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


async def test_inventory_duplicates_sort_and_pagination_preserve_ownership(isolated_database):
    from summon.repositories.catalog import CatalogRepository

    _client, db = isolated_database
    now = datetime.now(UTC)
    await db.inventory.insert_many(
        [
            {
                "user_id": 11,
                "character_id": "aiko",
                "quantity": 5,
                "favorite": True,
                "acquired_at": now,
            },
            {
                "user_id": 11,
                "character_id": "ren",
                "quantity": 5,
                "favorite": False,
                "acquired_at": now,
            },
            {
                "user_id": 11,
                "character_id": "mira",
                "quantity": 2,
                "favorite": True,
                "acquired_at": now,
            },
            {
                "user_id": 22,
                "character_id": "aiko",
                "quantity": 99,
                "favorite": True,
                "acquired_at": now,
            },
        ]
    )
    repo = CatalogRepository(db)
    first = await repo.inventory(11, duplicates=True, sort="copies", page=1, limit=2)
    second = await repo.inventory(11, duplicates=True, sort="copies", page=2, limit=2)
    assert [(row["id"], row["quantity"]) for row in first["items"]] == [("aiko", 5), ("ren", 5)]
    assert [(row["id"], row["quantity"]) for row in second["items"]] == [("mira", 2)]
    assert first["total"] == second["total"] == 3
    favorite = await repo.inventory(11, favorite=True, duplicates=True, sort="copies")
    assert [row["id"] for row in favorite["items"]] == ["aiko", "mira"]
    assert (await db.inventory.find_one({"user_id": 22, "character_id": "aiko"}))["quantity"] == 99
    assert (
        sum(row["quantity"] for row in await db.inventory.find({"user_id": 11}).to_list(length=10))
        == 12
    )


async def test_auto_reply_concurrent_edits_and_audit_rollback(isolated_database):
    from types import SimpleNamespace

    from summon.auto_replies import AutoReplyConflict
    from summon.repositories.auto_replies import AutoReplyRepository

    client, db = isolated_database
    now = datetime.now(UTC).replace(microsecond=0)
    await db.groups.insert_many(
        [
            {
                "_id": -100,
                "title": "Replies group",
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
    repo = AutoReplyRepository(client, db)
    original = await repo.save(-100, 42, "hello", "Hello", "Original", True, None, now)
    results = await asyncio.gather(
        repo.save(-100, 42, "hello", "hello", "First", True, original["revision"], now),
        repo.save(-100, 43, "hello", "hello", "Second", False, original["revision"], now),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, AutoReplyConflict) for result in results) == 1
    winner = next(result for result in results if isinstance(result, dict))
    assert await db.activity.count_documents({"group_id": -100, "kind": "auto_reply_changed"}) == 2
    with pytest.raises(AutoReplyConflict):
        await repo.delete(-100, 42, "hello", original["revision"], now)
    with pytest.raises(AutoReplyConflict):
        await repo.delete(-200, 42, "hello", winner["revision"], now)

    class FailingActivity:
        async def insert_one(self, *args, **kwargs):
            await db.activity.insert_one(*args, **kwargs)
            raise RuntimeError("injected audit failure")

    failing = AutoReplyRepository(
        client, SimpleNamespace(groups=db.groups, activity=FailingActivity())
    )
    with pytest.raises(RuntimeError, match="injected"):
        await failing.save(-100, 42, "hello", "hello", "Rolled back", True, winner["revision"], now)
    assert (await repo.list(-100))[0] == winner
    with pytest.raises(RuntimeError, match="injected"):
        await failing.delete(-100, 42, "hello", winner["revision"], now)
    assert (await repo.list(-100))[0] == winner
    assert await db.activity.count_documents({"group_id": -100, "kind": "auto_reply_changed"}) == 2
    for audit in await db.activity.find({"kind": "auto_reply_changed"}).to_list(length=10):
        assert "trigger" not in audit and "response" not in audit


async def test_auto_reply_normalized_trigger_uniqueness_is_atomic(isolated_database):
    from summon.auto_replies import AutoReplyConflict
    from summon.repositories.auto_replies import AutoReplyRepository

    client, db = isolated_database
    now = datetime.now(UTC)
    await db.groups.insert_one(
        {
            "_id": -100,
            "title": "Replies group",
            "settings": GroupSettings().model_dump(),
            "message_count": 0,
        }
    )
    repo = AutoReplyRepository(client, db)
    results = await asyncio.gather(
        repo.save(-100, 42, "one", "HELLO Straße", "First", True, None, now),
        repo.save(-100, 43, "two", " hello  STRASSE ", "Second", False, None, now),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, AutoReplyConflict) for result in results) == 1
    assert len(await repo.list(-100)) == 1
    assert await db.activity.count_documents({"group_id": -100, "kind": "auto_reply_changed"}) == 1


async def test_auto_reply_cap_and_schema_validation_are_enforced(isolated_database):
    from pymongo.errors import WriteError

    from summon.auto_replies import AutoReplyLimitError
    from summon.repositories.auto_replies import AutoReplyRepository

    client, db = isolated_database
    now = datetime.now(UTC)
    await db.groups.insert_one(
        {
            "_id": -100,
            "title": "Replies group",
            "settings": GroupSettings().model_dump(),
            "message_count": 0,
            "auto_replies_v1": {
                f"r{i}": {
                    "trigger": f"trigger {i}",
                    "response": "Example",
                    "enabled": False,
                    "revision": "a" * 32,
                    "created_at": now,
                    "created_by": 42,
                    "updated_at": now,
                    "updated_by": 42,
                }
                for i in range(19)
            },
        }
    )
    repo = AutoReplyRepository(client, db)
    results = await asyncio.gather(
        repo.save(-100, 42, "new-a", "hello one", "First", True, None, now),
        repo.save(-100, 43, "new-b", "hello two", "Second", True, None, now),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, AutoReplyLimitError) for result in results) == 1
    assert len(await repo.list(-100)) == 20
    assert await db.activity.count_documents({"group_id": -100, "kind": "auto_reply_changed"}) == 1
    # Direct malformed writes do not bypass the installed Mongo schema.
    with pytest.raises(WriteError):
        await db.groups.update_one({"_id": -100}, {"$set": {"auto_replies_v1.r0.enabled": "yes"}})
    assert len(await repo.list(-100)) == 20


async def test_auto_reply_delivery_claim_is_shared_across_workers(isolated_database):
    from summon.repositories.limits import RateLimiter

    _client, db = isolated_database
    one, two = RateLimiter(db), RateLimiter(db)
    key = "group:-100:auto-reply:message:123"
    results = await asyncio.gather(
        one.claim_once(key, seconds=48 * 60 * 60),
        two.claim_once(key, seconds=48 * 60 * 60),
    )
    assert sorted(results) == [False, True]
    assert not await RateLimiter(db).claim_once(key, seconds=48 * 60 * 60)
    rows = await db.rate_limits.find({"_id": {"$regex": "^once:"}}).to_list(length=5)
    assert len(rows) == 1 and "-100" not in rows[0]["_id"]
    assert rows[0]["expires_at"] > datetime.now(UTC) + timedelta(hours=47)


async def _wait_for_majority_poll_observation(repo):
    # Publisher writes use w=1; a majority read may briefly lag even after
    # a successful write, and that conservative delay is expected behavior.
    async with asyncio.timeout(5):
        while not await repo.is_recent(120):  # noqa: ASYNC110 - remote snapshot has no event
            await asyncio.sleep(0.05)


async def test_optional_polling_health_schema_and_server_clock(isolated_database):
    from pymongo.errors import WriteError
    from pymongo.write_concern import WriteConcern

    from summon.repositories.poller_health import PollerHealthRepository, poller_health_key

    _client, db = isolated_database
    bot, secret = "123456:FAKE_INTEGRATION_BOT", "s" * 40
    assert "summon_poller_heartbeats_v1" not in await db.list_collection_names()
    await initialize_database(db, include_poller_health=True)
    index_info = await db.summon_poller_heartbeats_v1.index_information()
    assert any(index.get("expireAfterSeconds") == 0 for index in index_info.values())
    repo = PollerHealthRepository(db, bot, secret)
    key = poller_health_key(bot, secret)
    # Database.command decodes this BSON UTC field as naive even with tz_aware.
    now = (await db.command("hello"))["localTime"].replace(tzinfo=UTC)
    assert await repo.is_recent(120) is False

    # Far-future first observations cannot become fresh later when time catches up.
    await repo.record(now + timedelta(minutes=10))
    assert await repo.is_recent(120) is False
    doc = await db.summon_poller_heartbeats_v1.find_one({"_id": key})
    if doc is not None:  # TTL may remove the expired sentinel asynchronously.
        assert doc["observed_at"] < now - timedelta(hours=1)

    await repo.record(now - timedelta(minutes=3))
    assert await repo.is_recent(120) is False
    await repo.record(now)
    await _wait_for_majority_poll_observation(repo)
    doc = await db.summon_poller_heartbeats_v1.find_one({"_id": key})
    assert set(doc) == {"_id", "observed_at", "expires_at"}
    assert doc["observed_at"] == now and doc["expires_at"] == now + timedelta(hours=1)
    assert bot not in str(doc) and secret not in str(doc)
    assert await PollerHealthRepository(db, "different-fake-bot", secret).is_recent(120) is False

    # Validator rejects extra payload data and malformed identifiers.
    with pytest.raises(WriteError):
        await db.summon_poller_heartbeats_v1.insert_one(
            {"_id": "unapproved", "observed_at": now, "expires_at": now, "payload": "private"}
        )
    # Freshness is checked independently of TTL and also rejects a future DB timestamp.
    durable = db.summon_poller_heartbeats_v1.with_options(
        write_concern=WriteConcern(w="majority", wtimeout=5000)
    )
    await durable.update_one({"_id": key}, {"$set": {"observed_at": now + timedelta(minutes=10)}})
    assert await repo.is_recent(120) is False
    await durable.update_one({"_id": key}, {"$set": {"observed_at": now - timedelta(minutes=3)}})
    assert await repo.is_recent(120) is False


async def test_poller_health_out_of_order_and_future_writes_fail_safely(isolated_database):
    from summon.repositories.poller_health import PollerHealthRepository, poller_health_key

    _client, db = isolated_database
    await initialize_database(db, include_poller_health=True)
    bot, secret = "123456:FAKE_INTEGRATION_BOT", "s" * 40
    reader = PollerHealthRepository(db, bot, secret)
    writer = PollerHealthRepository(db, bot, secret)
    now = (await db.command("hello"))["localTime"].replace(tzinfo=UTC)
    await writer.record(now - timedelta(minutes=3))
    await asyncio.gather(
        writer.record(now - timedelta(seconds=1)),
        writer.record(now),
        writer.record(now - timedelta(minutes=2)),
        writer.record(now + timedelta(minutes=10)),
    )
    doc = await db.summon_poller_heartbeats_v1.find_one({"_id": poller_health_key(bot, secret)})
    assert doc["observed_at"] == now
    assert doc["expires_at"] == now + timedelta(hours=1)
    await _wait_for_majority_poll_observation(reader)
    # A delayed write and an invalid forward-skewed clock neither advance the
    # event nor prolong its TTL; neither creates new polling progress.
    await writer.record(now - timedelta(minutes=3))
    await writer.record(now + timedelta(minutes=10))
    unchanged = await db.summon_poller_heartbeats_v1.find_one(
        {"_id": poller_health_key(bot, secret)}
    )
    assert unchanged == doc


@pytest.mark.parametrize(
    "sort,favorite,duplicates",
    [("recent", False, False), ("copies", False, False), ("copies", True, True)],
)
async def test_snapshot_inventory_traverses_650_stable_rows_with_mutations(
    isolated_database, sort, favorite, duplicates
):
    from pymongo.write_concern import WriteConcern

    from summon.repositories.catalog import CatalogRepository
    from summon.repositories.inventory_paging import CursorInvalid

    client, db = isolated_database
    durable_characters = db.characters.with_options(write_concern=WriteConcern(w="majority"))
    durable_inventory = db.inventory.with_options(write_concern=WriteConcern(w="majority"))
    now = datetime(2026, 1, 1, tzinfo=UTC)
    characters = [
        {
            "_id": f"snapshot-{i:04d}",
            "name": f"Snapshot {i}",
            "name_key": f"snapshot {i}",
            "category": "Synthetic",
            "rarity": "rare",
            "active": True,
            "random_key": 0.5,
        }
        for i in range(650)
    ]
    rows = [
        {
            "user_id": 11,
            "character_id": f"snapshot-{i:04d}",
            "quantity": 1 + i % 4,
            "favorite": i % 2 == 0,
            **({"acquired_at": now + timedelta(seconds=i % 5)} if i % 7 else {}),
        }
        for i in range(650)
    ]
    await durable_characters.insert_many(characters)
    await durable_inventory.insert_many(rows)
    await durable_inventory.insert_one(
        {"user_id": 22, "character_id": "snapshot-0000", "quantity": 99}
    )
    repo = CatalogRepository(db, client=client)
    secret = "synthetic-integration-secret-that-is-long-enough"
    query = dict(secret=secret, limit=24, sort=sort, favorite=favorite, duplicates=duplicates)
    first = await repo.inventory_cursor(11, **query)
    selected = [
        row
        for row in rows
        if (not favorite or row["favorite"]) and (not duplicates or row["quantity"] > 1)
    ]
    selected.sort(key=lambda row: row["character_id"])
    if sort == "recent":
        selected.sort(
            key=lambda row: row.get("acquired_at", datetime.min.replace(tzinfo=UTC)), reverse=True
        )
    else:
        selected.sort(key=lambda row: row["quantity"], reverse=True)
    assert first["total"] == len(selected)
    assert first["page"] == 1 and first["next_cursor"] and first["page_cursor"]
    with pytest.raises(CursorInvalid):
        await repo.inventory_cursor(22, cursor=first["next_cursor"], **query)
    with pytest.raises(CursorInvalid):
        await repo.inventory_cursor(
            11, cursor=first["next_cursor"], **{**query, "favorite": not favorite}
        )

    # Changes after the first page would move/filter rows in a live keyset query.
    await durable_characters.insert_one(
        {
            "_id": "snapshot-added",
            "name": "Added later",
            "name_key": "added later",
            "category": "Synthetic",
            "rarity": "rare",
            "active": True,
            "random_key": 0.5,
        }
    )
    await durable_inventory.insert_one(
        {
            "user_id": 11,
            "character_id": "snapshot-added",
            "quantity": 50,
            "favorite": True,
            "acquired_at": now + timedelta(days=1),
        }
    )
    await durable_inventory.update_one(
        {"user_id": 11, "character_id": selected[-1]["character_id"]},
        {
            "$set": {
                "quantity": 99,
                "favorite": not selected[-1]["favorite"],
                "acquired_at": now + timedelta(days=1),
            }
        },
    )
    await durable_characters.update_one(
        {"_id": selected[0]["character_id"]}, {"$set": {"name": "Changed later"}}
    )
    replay = await repo.inventory_cursor(11, cursor=first["page_cursor"], **query)
    assert replay["items"] == first["items"] and replay["total"] == first["total"]

    items = list(first["items"])
    page = first
    while page["next_cursor"]:
        page = await repo.inventory_cursor(11, cursor=page["next_cursor"], **query)
        items.extend(page["items"])
    assert [item["id"] for item in items] == [row["character_id"] for row in selected]
    assert [(item["quantity"], item["favorite"]) for item in items] == [
        (row["quantity"], row["favorite"]) for row in selected
    ]
    assert len({item["id"] for item in items}) == len(selected)
    assert all(item["id"] != "snapshot-added" for item in items)
    fresh = await repo.inventory_cursor(11, **query)
    assert fresh["total"] != first["total"] or fresh["items"] != first["items"]


@pytest.mark.parametrize(
    "bad,sort",
    [
        ({"character_id": "aiko", "quantity": 1, "acquired_at": "bad date"}, "recent"),
        ({"character_id": "aiko", "quantity": "2"}, "copies"),
        ({"character_id": "aiko", "quantity": 1.5}, "copies"),
        ({"character_id": "", "quantity": 1}, "recent"),
    ],
)
async def test_snapshot_inventory_rejects_legacy_corrupt_sort_values(isolated_database, bad, sort):
    from pymongo.write_concern import WriteConcern

    from summon.repositories.catalog import CatalogRepository
    from summon.repositories.inventory_paging import CursorDataError

    client, db = isolated_database
    await db.inventory.with_options(write_concern=WriteConcern(w="majority")).insert_one(
        {"user_id": 11, **bad}, bypass_document_validation=True
    )
    repo = CatalogRepository(db, client=client)
    with pytest.raises(CursorDataError):
        await repo.inventory_cursor(
            11, secret="synthetic-integration-secret-long-enough", sort=sort
        )


async def test_catalog_artwork_transactions_preserve_inventory_and_reject_replays(
    isolated_database,
):
    from io import BytesIO

    from PIL import Image

    from summon.models import CharacterChanges, CharacterInput
    from summon.repositories.catalog import CatalogConflict, CatalogNotFound, CatalogRepository
    from summon.services.artwork import normalize_photo

    client, db = isolated_database
    buffer = BytesIO()
    Image.new("RGB", (16, 16), "purple").save(buffer, format="JPEG")
    image = await normalize_photo(buffer.getvalue())
    repository = CatalogRepository(db, client=client)
    character = CharacterInput(
        id="portrait-test",
        name="Portrait",
        category="Synthetic",
        rarity="rare",
        image_url=image.path,
    )
    assert not await repository.character_exists(character.id)
    assert not await repository.catalog_event_exists("synthetic-create")
    await repository.create_character(
        character, actor_id=42, event_id="synthetic-create", artwork=image
    )
    assert await repository.character_exists(character.id)
    assert await repository.catalog_event_exists("synthetic-create")
    assert (await db.characters.find_one({"_id": character.id}))["image_url"] == image.path
    assert await repository.artwork(image.digest) == image.content
    assert (await db.activity.find_one({"_id": "synthetic-create"}))["user_id"] == 42

    await db.inventory.insert_one({"user_id": 101, "character_id": character.id, "quantity": 3})
    await repository.update_character(
        character.id, CharacterChanges(name="Updated"), actor_id=42, event_id="synthetic-update"
    )
    with pytest.raises(CatalogConflict):
        await repository.update_character(
            character.id,
            CharacterChanges(name="Replayed"),
            actor_id=42,
            event_id="synthetic-update",
        )
    assert (await db.characters.find_one({"_id": character.id}))["name"] == "Updated"
    assert (await db.inventory.find_one({"user_id": 101, "character_id": character.id}))[
        "quantity"
    ] == 3

    second_buffer = BytesIO()
    Image.new("RGB", (16, 16), "orange").save(second_buffer, format="JPEG")
    second_image = await normalize_photo(second_buffer.getvalue())
    with pytest.raises(CatalogNotFound):
        await repository.update_character(
            "missing",
            CharacterChanges(image_url=second_image.path),
            actor_id=42,
            event_id="synthetic-missing",
            artwork=second_image,
        )
    assert await db.activity.find_one({"_id": "synthetic-missing"}) is None
    assert await db.character_artworks.find_one({"_id": second_image.digest}) is None

    duplicate = character.model_copy(update={"image_url": second_image.path})
    with pytest.raises(CatalogConflict):
        await repository.create_character(
            duplicate, actor_id=42, event_id="synthetic-duplicate-id", artwork=second_image
        )
    assert await db.activity.find_one({"_id": "synthetic-duplicate-id"}) is None
    assert await db.character_artworks.find_one({"_id": second_image.digest}) is None
    assert (await db.inventory.find_one({"user_id": 101, "character_id": character.id}))[
        "quantity"
    ] == 3


async def test_native_ingress_lease_serializes_competitors_on_replica_set(isolated_database):
    from summon.bot.ingress import IngressLease, IngressLeaseLost

    _, db = isolated_database
    leases = [IngressLease(db, 123456), IngressLease(db, 123456)]
    results = await asyncio.gather(*(lease.acquire() for lease in leases), return_exceptions=True)
    winners = [index for index, result in enumerate(results) if result is None]
    assert len(winners) == 1
    winner = leases[winners[0]]
    loser = leases[1 - winners[0]]
    assert isinstance(results[1 - winners[0]], IngressLeaseLost)
    assert winner.valid() and not loser.valid()
    await winner.renew()
    await loser.release()  # Releasing a different owner cannot delete the lease.
    with pytest.raises(IngressLeaseLost):
        await loser.acquire()
    assert winner.valid()
    await winner.release()
    await loser.acquire()
    assert loser.valid()
    await loser.release()


async def test_hmode_is_actor_scoped_and_preserves_existing_points(isolated_database):
    client, db = isolated_database
    repository = UserRepository(client, db)
    actor = TelegramUser(id=987, first_name="Collector")
    now = datetime.now(UTC)
    await repository.touch(actor, now)
    await db.users.update_one({"_id": actor.id}, {"$set": {"points": 120, "xp": 45}})
    assert await repository.collection_mode(actor.id) == "recent"
    await repository.set_collection_mode(actor, "favorites", now)
    assert await repository.collection_mode(actor.id) == "favorites"
    assert await repository.collection_mode(actor.id + 1) == "recent"
    row = await db.users.find_one({"_id": actor.id})
    assert row["points"] == 120 and row["xp"] == 45
    assert await db.users.find_one({"_id": actor.id + 1}) is None


@pytest.mark.parametrize("blocked_by", ["disabled", "threshold", "cooldown", "active"])
async def test_manual_spawn_uses_same_atomic_group_gates(isolated_database, blocked_by):
    client, db = isolated_database
    now = datetime.now(UTC).replace(microsecond=0)
    settings = GroupSettings(spawn_enabled=True).model_dump()
    group = {
        "_id": -400,
        "title": "Isolated group",
        "settings": settings,
        "message_count": settings["spawn_every"],
        "next_spawn_at": now,
        "active_spawn": None,
    }
    if blocked_by == "disabled":
        settings["spawn_enabled"] = False
    elif blocked_by == "threshold":
        group["message_count"] -= 1
    elif blocked_by == "cooldown":
        group["next_spawn_at"] = now + timedelta(minutes=1)
    else:
        group["active_spawn"] = {"id": "existing", "expires_at": now + timedelta(minutes=1)}
    await db.groups.insert_one(group)
    character = await db.characters.find_one({"_id": "aiko"})
    assert character is not None
    repo = CollectionRepository(client, db)
    assert await repo.reserve_spawn(-400, character, now) is None
    stored = await db.groups.find_one({"_id": -400})
    assert stored["message_count"] == group["message_count"]
    assert stored["active_spawn"] == group["active_spawn"]
    assert await db.spawns.count_documents({"group_id": -400}) == 0


async def test_concurrent_manual_spawn_reservations_only_create_one(isolated_database):
    client, db = isolated_database
    now = datetime.now(UTC)
    settings = GroupSettings(spawn_enabled=True).model_dump()
    await db.groups.insert_one(
        {
            "_id": -400,
            "title": "Isolated group",
            "settings": settings,
            "message_count": settings["spawn_every"],
            "next_spawn_at": now,
            "active_spawn": None,
        }
    )
    character = await db.characters.find_one({"_id": "aiko"})
    assert character is not None
    repo = CollectionRepository(client, db)
    results = await asyncio.gather(*(repo.reserve_spawn(-400, character, now) for _ in range(5)))
    assert sum(result is not None for result in results) == 1
    assert await db.spawns.count_documents({"group_id": -400, "status": "active"}) == 1


async def test_spawn_weight_changes_and_audits_are_atomic_and_replay_safe(isolated_database):
    from pymongo.errors import WriteError

    from summon.rarities import RARITY_WEIGHTS
    from summon.repositories.catalog import CatalogRepository
    from summon.repositories.rarity_weights import SpawnWeightConflict

    client, db = isolated_database
    repo = CatalogRepository(db, client=client)
    assert await repo.spawn_weights() == RARITY_WEIGHTS
    await db.inventory.insert_one({"user_id": 101, "character_id": "ren", "quantity": 3})
    assert await repo.set_spawn_weight(
        "special-edition", 0, actor_id=42, event_id="weight-one"
    ) == (RARITY_WEIGHTS["special-edition"], 0)
    with pytest.raises(SpawnWeightConflict):
        await repo.set_spawn_weight("rare", 99, actor_id=42, event_id="weight-one")
    assert (await repo.spawn_weights())["rare"] == RARITY_WEIGHTS["rare"]
    assert await db.spawn_weights.find_one({"_id": "rare"}) is None
    assert await db.activity.count_documents({"_id": "weight-one"}) == 1
    assert (await db.characters.find_one({"_id": "ren"}))["rarity"] == "epic"
    assert (await db.inventory.find_one({"user_id": 101, "character_id": "ren"}))["quantity"] == 3

    # Direct malformed writes cannot bypass the installed MongoDB schema.
    with pytest.raises(WriteError):
        await db.spawn_weights.insert_one(
            {"_id": "rare", "weight": -1, "updated_by": 42, "updated_at": datetime.now(UTC)}
        )
    with pytest.raises(WriteError):
        await db.spawn_weights.insert_one(
            {"_id": "epic", "weight": 1, "updated_by": 42, "updated_at": datetime.now(UTC)}
        )


async def test_concurrent_spawn_weight_changes_preserve_independent_buckets(isolated_database):
    from summon.repositories.catalog import CatalogRepository

    client, db = isolated_database
    repo = CatalogRepository(db, client=client)
    results = await asyncio.gather(
        repo.set_spawn_weight("rare", 77, actor_id=42, event_id="rare-event"),
        repo.set_spawn_weight("legendary", 88, actor_id=43, event_id="legendary-event"),
    )
    assert [result[1] for result in results] == [77, 88]
    weights = await repo.spawn_weights()
    assert weights["rare"] == 77 and weights["legendary"] == 88
    assert await db.activity.count_documents({"kind": "spawn_weight_changed"}) == 2


async def test_real_spawn_selection_uses_overrides_and_preserves_legacy_epic(isolated_database):
    from summon.rarities import RARITY_WEIGHTS
    from summon.repositories.catalog import CatalogRepository

    client, db = isolated_database
    now = datetime.now(UTC)
    await db.spawn_weights.insert_many(
        [
            {
                "_id": name,
                "weight": 1 if name == "special-edition" else 0,
                "updated_by": 42,
                "updated_at": now,
            }
            for name in RARITY_WEIGHTS
        ]
    )
    repo = CatalogRepository(db, client=client)
    assert (await repo.choose())["_id"] == "ren"
    await db.spawn_weights.update_one({"_id": "special-edition"}, {"$set": {"weight": 0}})
    assert await repo.choose() is None
    assert (await db.characters.find_one({"_id": "ren"}))["rarity"] == "epic"


async def test_free_daily_character_is_once_per_actor_utc_day_and_preserves_daily_rewards(
    isolated_database,
):
    from summon.repositories.collection import DailyCharacterAlreadyClaimed

    client, db = isolated_database
    now = datetime(2026, 4, 5, 12, tzinfo=UTC)
    actor = TelegramUser(id=700, first_name="Collector")
    users = UserRepository(client, db)
    await users.touch(actor, now)
    await db.users.update_one({"_id": actor.id}, {"$set": {"points": 120, "xp": 45}})
    await users.daily(actor, now)
    before = await db.users.find_one({"_id": actor.id})
    await db.inventory.insert_one(
        {"user_id": actor.id, "character_id": "aiko", "quantity": 2, "favorite": True}
    )
    repo = CollectionRepository(client, db)
    results = await asyncio.gather(
        *(
            repo.claim_daily_character(
                actor, "aiko", now, message_id="daily-character-message:" + f"{i:064x}"
            )
            for i in range(5)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, DailyCharacterAlreadyClaimed) for result in results) == 4
    item = await db.inventory.find_one({"user_id": actor.id, "character_id": "aiko"})
    assert item["quantity"] == 3 and item["favorite"] is True
    after = await db.users.find_one({"_id": actor.id})
    for field in ("points", "xp", "last_daily", "streak", "catch_count"):
        assert after[field] == before[field]
    assert await db.reward_history.count_documents({"kind": "daily_character", "user_id": 700}) == 1
    assert await db.activity.count_documents({"kind": "daily_character", "user_id": 700}) == 1

    # A different actor and the next UTC day have separate guarded claims.
    other = TelegramUser(id=701, first_name="Other")
    assert (
        await repo.claim_daily_character(
            other, "aiko", now, message_id="daily-character-message:" + "a" * 64
        )
    )["id"] == "aiko"
    assert (
        await repo.claim_daily_character(
            actor, "aiko", now + timedelta(days=1), message_id="daily-character-message:" + "b" * 64
        )
    )["id"] == "aiko"
    assert (await db.inventory.find_one({"user_id": 700, "character_id": "aiko"}))["quantity"] == 4


async def test_free_daily_character_ledger_and_ownership_rollback_together(isolated_database):
    from types import SimpleNamespace

    client, db = isolated_database
    now = datetime(2026, 4, 5, 12, tzinfo=UTC)
    actor = TelegramUser(id=700, first_name="Collector")

    class FailingInventory:
        async def update_one(self, *args, **kwargs):
            await db.inventory.update_one(*args, **kwargs)
            raise RuntimeError("injected inventory failure")

    repo = CollectionRepository(
        client,
        SimpleNamespace(
            characters=db.characters,
            reward_history=db.reward_history,
            users=db.users,
            inventory=FailingInventory(),
            activity=db.activity,
        ),
    )
    with pytest.raises(RuntimeError, match="injected inventory failure"):
        await repo.claim_daily_character(
            actor, "aiko", now, message_id="daily-character-message:" + "c" * 64
        )
    assert await db.users.find_one({"_id": actor.id}) is None
    assert await db.inventory.find_one({"user_id": actor.id}) is None
    assert await db.reward_history.find_one({"_id": "daily-character:700:2026-04-05"}) is None
    assert await db.activity.find_one({"_id": "daily-character:700:2026-04-05"}) is None


async def test_free_daily_character_rejects_deactivated_character_without_consuming_day(
    isolated_database,
):
    from summon.repositories.collection import DailyCharacterUnavailable

    client, db = isolated_database
    now = datetime(2026, 4, 5, 12, tzinfo=UTC)
    actor = TelegramUser(id=700, first_name="Collector")
    await db.characters.update_one({"_id": "aiko"}, {"$set": {"active": False}})
    repo = CollectionRepository(client, db)
    with pytest.raises(DailyCharacterUnavailable):
        await repo.claim_daily_character(
            actor, "aiko", now, message_id="daily-character-message:" + "c" * 64
        )
    assert await db.reward_history.find_one({"_id": "daily-character:700:2026-04-05"}) is None
    assert (
        await repo.claim_daily_character(
            actor, "mira", now, message_id="daily-character-message:" + "d" * 64
        )
    )["id"] == "mira"


async def test_daily_character_same_message_cannot_be_replayed_across_midnight(isolated_database):
    from summon.repositories.collection import DailyCharacterAlreadyClaimed

    client, db = isolated_database
    actor = TelegramUser(id=700, first_name="Collector")
    repo = CollectionRepository(client, db)
    before_midnight = datetime(2026, 4, 5, 23, 59, tzinfo=UTC)
    message_id = "daily-character-message:" + "e" * 64
    await repo.claim_daily_character(actor, "aiko", before_midnight, message_id=message_id)
    with pytest.raises(DailyCharacterAlreadyClaimed):
        await repo.claim_daily_character(
            actor, "aiko", before_midnight + timedelta(minutes=2), message_id=message_id
        )
    assert await db.reward_history.find_one({"_id": "daily-character:700:2026-04-06"}) is None
    assert (await db.inventory.find_one({"user_id": 700, "character_id": "aiko"}))["quantity"] == 1
    await repo.claim_daily_character(
        actor,
        "aiko",
        before_midnight + timedelta(minutes=3),
        message_id="daily-character-message:" + "f" * 64,
    )
    assert (await db.inventory.find_one({"user_id": 700, "character_id": "aiko"}))["quantity"] == 2


async def test_numeric_check_backfills_aliases_without_changing_ownership(isolated_database):
    from summon.repositories.catalog import CatalogRepository

    client, db = isolated_database
    # Simulate a pre-numbering catalog in this explicitly isolated test DB.
    await db.catalog_counters.delete_one({"_id": "characters"})
    await db.characters.update_many({}, {"$unset": {"number": ""}})
    await db.inventory.insert_one(
        {"user_id": 700, "character_id": "aiko", "quantity": 2, "favorite": True}
    )
    repo = CatalogRepository(db, client=client)
    first, padded = await asyncio.gather(repo.details(700, "1"), repo.details(700, "01"))
    assert first["id"] == padded["id"] == "aiko"
    assert first["number"] == padded["number"] == 1
    assert first["quantity"] == padded["quantity"] == 2
    assert (await repo.details(700, "aiko"))["id"] == "aiko"
    assert (await db.inventory.find_one({"user_id": 700}))["character_id"] == "aiko"
    numbers = [item["number"] async for item in db.characters.find({})]
    assert sorted(numbers) == [1, 2, 3]


async def test_conflicting_numeric_legacy_slugs_fail_closed_without_renumbering(isolated_database):
    from summon.repositories.catalog import CatalogRepository
    from summon.repositories.character_numbers import CharacterNumberingUnavailable

    client, db = isolated_database
    await db.catalog_counters.delete_one({"_id": "characters"})
    await db.characters.update_many({}, {"$unset": {"number": ""}})
    template = await db.characters.find_one({"_id": "aiko"})
    for character_id in ("1", "01"):
        await db.characters.insert_one({**template, "_id": character_id})
    with pytest.raises(CharacterNumberingUnavailable):
        await CatalogRepository(db, client=client).details(700, "1")
    assert await db.catalog_counters.find_one({"_id": "characters"}) is None
    assert await db.characters.count_documents({"number": {"$exists": True}}) == 0


async def test_concurrent_new_characters_receive_distinct_stable_numbers(isolated_database):
    from summon.models import CharacterInput
    from summon.repositories.catalog import CatalogRepository

    client, db = isolated_database
    repo = CatalogRepository(db, client=client)
    await repo.details(700, "1")

    async def create(index):
        data = CharacterInput(
            id=f"new-{index}",
            name=f"New {index}",
            category="Sky",
            rarity="rare",
            image_url="https://example.com/image.jpg",
        )
        return await repo.create_character(data, actor_id=700, event_id=f"numeric-test-{index}")

    saved = await asyncio.gather(*(create(index) for index in range(5)))
    assert sorted(item["number"] for item in saved) == [4, 5, 6, 7, 8]
    for item in saved:
        assert (await repo.details(700, str(item["number"])))["id"] == item["id"]


async def test_encrypted_snapshot_and_insert_only_restore_into_isolated_database(
    isolated_database, tmp_path
):
    from summon.backup import (
        BackupError,
        _read_encrypted,
        export_snapshot,
        inspect_backup,
        restore_empty,
    )

    client, source = isolated_database
    await source.inventory.insert_one(
        {"user_id": 700, "character_id": "aiko", "quantity": 2, "favorite": True}
    )
    await source.users.insert_one(
        {"_id": 700, "first_name": "Private test player", "bot_token": "fake-secret"}
    )
    await source.web_sessions.insert_one(
        {
            "_id": "a" * 64,
            "user_id": 700,
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        }
    )
    path = tmp_path / "isolated.sbak"
    password = "a long isolated test passphrase"
    exported = await export_snapshot(client, source, path, password)
    encrypted = _read_encrypted(path)
    assert b"Private test player" not in encrypted and b"fake-secret" not in encrypted
    assert inspect_backup(encrypted, password)[1] == exported
    target_name = f"summon_restore_{uuid4().hex}"
    try:
        restored = await restore_empty(client, encrypted, password, target_name)
        target = client[target_name]
        assert restored == exported
        assert (await target.inventory.find_one({"user_id": 700}))["character_id"] == "aiko"
        assert (await target.characters.find_one({"_id": "aiko"}))["number"] == 1
        assert "bot_token" not in await target.users.find_one({"_id": 700})
        assert await target.web_sessions.count_documents({}) == 0
        with pytest.raises(BackupError, match="empty"):
            await restore_empty(client, encrypted, password, target_name)
        assert await target.inventory.count_documents({"user_id": 700}) == 1
    finally:
        if os.environ.get("SUMMON_TEST_MONGO_DROP_CONFIRM") == "DROP_ISOLATED_TEST_DB":
            await client.drop_database(target_name)


async def test_collection_insights_count_owned_active_favorites_rarities_and_orphans(
    isolated_database,
):
    from summon.repositories.collection_insights import CollectionInsightsRepository

    client, db = isolated_database
    await db.characters.update_one({"_id": "ren"}, {"$set": {"active": False}})
    await db.inventory.insert_many(
        [
            {"user_id": 701, "character_id": "aiko", "quantity": 3, "favorite": True},
            {"user_id": 701, "character_id": "ren", "quantity": 1, "favorite": False},
            {"user_id": 701, "character_id": "legacy-missing", "quantity": 2, "favorite": False},
            {"user_id": 702, "character_id": "mira", "quantity": 9, "favorite": True},
        ]
    )
    report = await CollectionInsightsRepository(client, db).read(701)
    assert report["unique_characters"] == 3 and report["total_copies"] == 6
    assert report["duplicate_copies"] == 3 and report["favorite_characters"] == 1
    assert report["active_owned"] == 1 and report["catalog_size"] == 2
    assert report["completion_percent"] == 50.0
    breakdown = {item["rarity"]: item for item in report["rarity_breakdown"]}
    assert breakdown["legendary"]["copies"] == 3
    assert breakdown["epic"]["unique"] == 1
    assert breakdown["unknown"]["copies"] == 2
    other = await CollectionInsightsRepository(client, db).read(702)
    assert other["unique_characters"] == 1 and other["total_copies"] == 9
    assert await db.inventory.count_documents({"user_id": 701}) == 3


async def test_legacy_best_streak_survives_first_new_daily_after_a_gap(isolated_database):
    client, db = isolated_database
    now = datetime(2026, 1, 10, tzinfo=UTC)
    await db.users.insert_one(
        {"_id": 701, "last_daily": "2026-01-01", "streak": 8, "points": 0, "xp": 0}
    )
    repo = UserRepository(client, db)
    reward = await repo.daily(TelegramUser(id=701, first_name="Test"), now)
    assert reward["streak"] == 1
    row = await db.users.find_one({"_id": 701})
    assert row["streak"] == 1 and row["best_streak"] == 8
    await repo.daily(TelegramUser(id=701, first_name="Test"), now + timedelta(days=1))
    row = await db.users.find_one({"_id": 701})
    assert row["streak"] == 2 and row["best_streak"] == 8


async def test_first_encounter_remains_after_historical_inventory_reaches_zero(isolated_database):
    client, db = isolated_database
    await db.inventory.insert_one(
        {"user_id": 703, "character_id": "aiko", "quantity": 0, "favorite": False}
    )
    repo = UserRepository(client, db)
    progress = await repo.progression(703, datetime(2026, 1, 10, tzinfo=UTC))
    first = next(item for item in progress["achievements"] if item["id"] == "first-summon")
    assert first["completed"] is True
    assert await db.inventory.count_documents({"user_id": 703, "quantity": {"$gt": 0}}) == 0


async def test_reviewed_catalog_writes_detect_concurrent_edits_and_preserve_ownership(
    isolated_database,
):
    from summon.models import CharacterChanges, CharacterInput
    from summon.repositories.catalog import CatalogRepository, CatalogStaleRevision

    client, db = isolated_database
    repository = CatalogRepository(db, client=client)
    secret = "test-catalog-revision-secret-" + "x" * 32
    await db.inventory.insert_one({"user_id": 101, "character_id": "aiko", "quantity": 3})
    initial = await repository.edit_state("aiko", secret=secret)
    first = CharacterInput(
        id="aiko", name="Reviewed one", category="Sky", rarity="rare", image_url="/art/aiko.webp"
    )
    second = CharacterInput(
        id="aiko", name="Reviewed two", category="Sky", rarity="rare", image_url="/art/aiko.webp"
    )
    outcomes = await asyncio.gather(
        repository.upsert_character(
            first, actor_id=10, expected_revision=initial["revision"], revision_secret=secret
        ),
        repository.upsert_character(
            second, actor_id=11, expected_revision=initial["revision"], revision_secret=secret
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in outcomes) == 1
    assert sum(isinstance(result, CatalogStaleRevision) for result in outcomes) == 1
    stored = await db.characters.find_one({"_id": "aiko"})
    assert stored["name"] in {"Reviewed one", "Reviewed two"}
    assert (await db.inventory.find_one({"user_id": 101, "character_id": "aiko"}))["quantity"] == 3
    assert (
        await db.activity.count_documents({"kind": "character_saved", "character_id": "aiko"}) == 1
    )

    # A Telegram-style partial edit after review also invalidates the reviewed save.
    state = await repository.edit_state("aiko", secret=secret)
    await repository.update_character(
        "aiko", CharacterChanges(name="Telegram edit"), actor_id=12, event_id="review-test-telegram"
    )
    with pytest.raises(CatalogStaleRevision):
        await repository.upsert_character(
            first, actor_id=10, expected_revision=state["revision"], revision_secret=secret
        )
    assert (await db.characters.find_one({"_id": "aiko"}))["name"] == "Telegram edit"

    # Two operators reviewing a previously missing ID cannot replace one another.
    new_state = await repository.edit_state("review-new", secret=secret)
    assert new_state["character"] is None
    new_one = CharacterInput(
        id="review-new",
        name="Created one",
        category="Sky",
        rarity="rare",
        image_url="/art/aiko.webp",
    )
    new_two = CharacterInput(
        id="review-new",
        name="Created two",
        category="Sky",
        rarity="rare",
        image_url="/art/aiko.webp",
    )
    created = await asyncio.gather(
        repository.upsert_character(
            new_one, actor_id=10, expected_revision=new_state["revision"], revision_secret=secret
        ),
        repository.upsert_character(
            new_two, actor_id=11, expected_revision=new_state["revision"], revision_secret=secret
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in created) == 1
    assert sum(isinstance(result, CatalogStaleRevision) for result in created) == 1
    assert await db.characters.count_documents({"_id": "review-new"}) == 1
    assert (
        await db.activity.count_documents({"kind": "character_saved", "character_id": "review-new"})
        == 1
    )


async def test_group_summon_insights_are_scoped_and_classify_recorded_states(isolated_database):
    from summon.repositories.group_insights import GroupInsightsDataError

    client, db = isolated_database
    now = datetime.now(UTC)
    base = {"group_id": -100, "created_at": now - timedelta(hours=1)}
    await db.spawns.insert_many(
        [
            {"_id": "mine", **base, "status": "claimed", "claimed_by": 101},
            {"_id": "theirs", **base, "status": "claimed", "claimed_by": 102},
            {
                "_id": "expired-active",
                **base,
                "status": "active",
                "expires_at": now - timedelta(seconds=1),
            },
            {"_id": "expired", **base, "status": "expired"},
            {
                "_id": "awaiting",
                **base,
                "status": "active",
                "expires_at": now + timedelta(minutes=1),
            },
            {"_id": "rejected", **base, "status": "delivery_failed"},
            {
                "_id": "old",
                **base,
                "created_at": now - timedelta(days=31),
                "status": "claimed",
                "claimed_by": 101,
            },
            {
                "_id": "future",
                **base,
                "created_at": now + timedelta(days=1),
                "status": "claimed",
                "claimed_by": 101,
            },
            {
                "_id": "other-group",
                **base,
                "group_id": -200,
                "status": "claimed",
                "claimed_by": 101,
            },
        ]
    )
    repo = CollectionRepository(client, db)
    report = await repo.group_summon_insights(-100, 101, now)
    assert {
        key: report[key]
        for key in (
            "attempts",
            "caught",
            "expired",
            "delivery_rejected",
            "awaiting_resolution",
            "my_catches",
        )
    } == {
        "attempts": 6,
        "caught": 2,
        "expired": 2,
        "delivery_rejected": 1,
        "awaiting_resolution": 1,
        "my_catches": 1,
    }
    assert (await repo.group_summon_insights(-200, 101, now))["my_catches"] == 1
    assert (await repo.group_summon_insights(-300, 101, now))["attempts"] == 0
    await db.spawns.insert_one({"_id": "malformed", **base, "status": "active"})
    with pytest.raises(GroupInsightsDataError):
        await repo.group_summon_insights(-100, 101, now)


async def test_reviewed_catalog_video_metadata_preserves_or_clears_file_reference(
    isolated_database,
):
    from summon.models import CharacterInput
    from summon.repositories.catalog import CatalogRepository

    client, db = isolated_database
    repo = CatalogRepository(db, client=client)
    secret = "synthetic-review-secret-" + "x" * 40
    video_url = "https://files.catbox.moe/synthetic.mp4"
    original = CharacterInput(
        id="review-video",
        name="Motion",
        category="Synthetic",
        rarity="amv-edition",
        image_url=video_url,
        media_type="video",
        telegram_file_id="synthetic-telegram-file-id",
    )
    await repo.upsert_character(original, actor_id=101)
    await db.inventory.insert_one({"user_id": 102, "character_id": original.id, "quantity": 2})
    state = await repo.edit_state(original.id, secret=secret)
    assert state["character"]["media_type"] == "video"
    assert "telegram_file_id" not in state["character"]
    metadata = CharacterInput(
        id=original.id,
        name="Motion revised",
        category="Synthetic",
        rarity="amv-edition",
        image_url=video_url,
    )
    await repo.upsert_character(
        metadata, actor_id=101, expected_revision=state["revision"], revision_secret=secret
    )
    stored = await db.characters.find_one({"_id": original.id})
    assert (
        stored["media_type"] == "video"
        and stored["telegram_file_id"] == "synthetic-telegram-file-id"
    )
    state = await repo.edit_state(original.id, secret=secret)
    replacement = metadata.model_copy(update={"image_url": "https://i.ibb.co/example/new.jpg"})
    await repo.upsert_character(
        replacement, actor_id=101, expected_revision=state["revision"], revision_secret=secret
    )
    stored = await db.characters.find_one({"_id": original.id})
    assert stored["media_type"] == "photo" and stored["telegram_file_id"] is None
    assert (await db.inventory.find_one({"user_id": 102, "character_id": original.id}))[
        "quantity"
    ] == 2
