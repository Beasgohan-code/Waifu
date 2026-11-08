"""Persistent session records store hashes, enforce expiry, and fail closed."""

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from summon.database import COLLECTION_SCHEMAS, INDEXES
from summon.repositories.sessions import SessionRepository


def repository():
    collection = MagicMock()
    collection.with_options.return_value = collection
    collection.insert_one = AsyncMock()
    collection.find_one = AsyncMock()
    collection.delete_one = AsyncMock()
    return SessionRepository(SimpleNamespace(web_sessions=collection)), collection


def test_session_schema_and_ttl_are_declared():
    assert set(COLLECTION_SCHEMAS["web_sessions"]["required"]) == {"_id", "user_id", "expires_at"}
    assert any(
        keys == [("expires_at", 1)] and options.get("expireAfterSeconds") == 0
        for keys, options in INDEXES["web_sessions"]
    )


async def test_session_records_use_hashed_ids_majority_writes_and_explicit_expiry():
    repo, collection = repository()
    now = datetime.now(UTC)
    raw_id = "test-session-id-not-a-real-credential"
    await repo.create(raw_id, 123, now, 300)
    document = collection.insert_one.await_args.args[0]
    assert document == {
        "_id": hashlib.sha256(raw_id.encode()).hexdigest(),
        "user_id": 123,
        "expires_at": now + timedelta(seconds=300),
    }
    options = collection.with_options.call_args.kwargs
    assert options["write_concern"].document["w"] == "majority"
    assert options["read_concern"].level == "linearizable"
    assert options["read_preference"].name == "Primary"


async def test_session_checks_exact_id_owner_and_expiry_with_a_bounded_linearizable_read():
    repo, collection = repository()
    now = datetime.now(UTC)
    raw_id = "test-session-id-not-a-real-credential"
    hashed = hashlib.sha256(raw_id.encode()).hexdigest()
    collection.find_one.return_value = {"_id": hashed}
    assert await repo.is_active(raw_id, 123, now)
    query = collection.find_one.await_args.args[0]
    assert query == {"_id": hashed, "user_id": 123, "expires_at": {"$gt": now}}
    assert collection.find_one.await_args.kwargs["max_time_ms"] > 0
    collection.find_one.return_value = None
    assert not await repo.is_active(raw_id, 123, now)


async def test_revocation_deletes_only_the_matching_owner_session():
    repo, collection = repository()
    raw_id = "test-session-id-not-a-real-credential"
    await repo.revoke(raw_id, 123)
    collection.delete_one.assert_awaited_once_with(
        {"_id": hashlib.sha256(raw_id.encode()).hexdigest(), "user_id": 123}
    )
