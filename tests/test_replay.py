from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pymongo.errors import DuplicateKeyError

from summon.repositories.auth import AuthRepository


async def test_init_data_signature_is_consumed_atomically_once():
    collection = MagicMock()
    collection.with_options.return_value = collection
    collection.insert_one = AsyncMock(side_effect=[None, DuplicateKeyError("duplicate")])
    db = SimpleNamespace(auth_nonces=collection)
    repo = AuthRepository(db)
    now = datetime.now(UTC)
    assert await repo.consume("abc", now, 300)
    assert not await repo.consume("abc", now, 300)
    doc = collection.insert_one.await_args_list[0].args[0]
    assert doc["_id"] != "abc"  # Don't store the raw signed proof.
    # Integer-second timestamp checks permit the full final second; a future-
    # tolerated proof must not be deleted while it could still authenticate.
    assert doc["expires_at"] >= now + timedelta(seconds=331)
    assert collection.with_options.call_args.kwargs["write_concern"].document["w"] == "majority"
