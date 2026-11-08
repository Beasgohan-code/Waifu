"""Offline report, snapshot-query and actor-isolation regression tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from summon.repositories.collection_insights import (
    CollectionInsightsRepository,
    InsightsDataError,
    summarize_collection,
)


def bucket(rarity, unique, copies, favorites, active):
    return {
        "_id": rarity,
        "unique": unique,
        "copies": copies,
        "favorites": favorites,
        "active_owned": active,
    }


def test_report_counts_copies_favorites_active_and_legacy_rarities():
    result = summarize_collection(
        [bucket("epic", 1, 3, 1, 0), bucket("rare", 2, 3, 1, 2), bucket("unknown", 1, 2, 0, 0)],
        catalog_size=4,
    )
    assert result["unique_characters"] == 4
    assert result["total_copies"] == 8
    assert result["duplicate_copies"] == 4
    assert result["favorite_characters"] == 2
    assert result["active_owned"] == 2 and result["completion_percent"] == 50.0
    assert [row["rarity"] for row in result["rarity_breakdown"]] == ["rare", "epic", "unknown"]
    goals = {goal["id"]: goal for goal in result["goals"]}
    assert goals["collection-1"]["completed"] is True
    assert goals["collection-5"]["current"] == 4 and not goals["collection-5"]["completed"]
    assert goals["favorites-5"]["current"] == 2


def test_empty_catalog_has_no_percentage_and_reversible_current_goals():
    result = summarize_collection([], 0)
    assert result["completion_percent"] is None
    assert result["total_copies"] == 0 and not result["rarity_breakdown"]
    assert all(not goal["completed"] for goal in result["goals"])


@pytest.mark.parametrize(
    "rows, catalog",
    [
        ([bucket("rare", 2, 1, 0, 1)], 3),
        ([bucket("rare", 1, 1, 2, 1)], 3),
        ([bucket("rare", 1, 1, 0, 1)], 0),
        ([bucket("unlisted-rarity", 1, 1, 0, 1)], 3),
        ([bucket("rare", 1, 1, 0, 1), bucket("rare", 1, 1, 0, 1)], 3),
        ([bucket("rare", 1, 2**53, 0, 1)], 3),
        ([bucket("rare", True, 1, 0, 1)], 3),
    ],
)
def test_inconsistent_or_unrepresentable_aggregates_fail_closed(rows, catalog):
    with pytest.raises(InsightsDataError):
        summarize_collection(rows, catalog)


async def test_repository_uses_one_bounded_snapshot_and_the_requested_actor():
    session = object()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    client = SimpleNamespace(start_session=MagicMock(return_value=context))
    cursor = SimpleNamespace(to_list=AsyncMock(return_value=[bucket("rare", 1, 2, 1, 1)]))
    inventory = SimpleNamespace(aggregate=AsyncMock(return_value=cursor))
    characters = SimpleNamespace(count_documents=AsyncMock(return_value=3))
    result = await CollectionInsightsRepository(
        client, SimpleNamespace(inventory=inventory, characters=characters)
    ).read(42)
    assert result["total_copies"] == 2
    client.start_session.assert_called_once_with(snapshot=True, causal_consistency=False)
    pipeline = inventory.aggregate.await_args.args[0]
    assert pipeline[0] == {"$match": {"user_id": 42, "quantity": {"$gt": 0}}}
    assert inventory.aggregate.await_args.kwargs["session"] is session
    assert inventory.aggregate.await_args.kwargs["allowDiskUse"] is False
    assert characters.count_documents.await_args.kwargs["session"] is session


@pytest.mark.parametrize("actor", [0, -1, True, 2**53, "42"])
async def test_invalid_actor_is_rejected_without_queries(actor):
    client = SimpleNamespace(start_session=MagicMock())
    with pytest.raises(ValueError):
        await CollectionInsightsRepository(client, object()).read(actor)
    client.start_session.assert_not_called()
