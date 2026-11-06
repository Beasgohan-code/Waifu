"""Optional read-only probe of an explicitly authorized standalone MongoDB."""

from __future__ import annotations

import os

import pytest
from pymongo import AsyncMongoClient

from summon.database import DatabaseTopologyError, require_transaction_topology

pytestmark = pytest.mark.integration


async def test_standalone_topology_is_rejected_without_schema_writes():
    uri = os.environ.get("SUMMON_TEST_STANDALONE_URI")
    if not uri:
        pytest.skip("Set an authorized standalone test URI to run this read-only probe")
    if os.environ.get("SUMMON_TEST_STANDALONE_CONFIRM") != "PROBE_STANDALONE_READ_ONLY":
        pytest.fail("Explicit confirmation is required for the standalone read-only probe")
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        await client.admin.command("ping")
        with pytest.raises(DatabaseTopologyError):
            await require_transaction_topology(client)
    finally:
        await client.close()
