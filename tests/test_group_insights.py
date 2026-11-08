"""Privacy and validation for rolling group summon insights."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from summon.repositories.group_insights import (
    GroupInsightsDataError,
    GroupInsightsRepository,
    summarize_group_summons,
)

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


def row(**overrides):
    return {
        "attempts": 8,
        "caught": 4,
        "expired": 2,
        "delivery_rejected": 1,
        "awaiting_resolution": 1,
        "my_catches": 2,
        **overrides,
    }


def test_group_summary_is_bounded_and_includes_only_own_catches():
    report = summarize_group_summons(row(), NOW)
    assert report == {"window_days": 30, "as_of": NOW, **row()}
    assert set(summarize_group_summons(None, NOW)) == set(report)
    assert summarize_group_summons(None, NOW)["attempts"] == 0
    assert not any("user" in key or "name" in key for key in report)


@pytest.mark.parametrize(
    "bad",
    [
        row(attempts=9),
        row(my_catches=5),
        row(caught=True),
        row(expired=-1),
        row(delivery_rejected=2**53),
        {"attempts": 1},
    ],
)
def test_inconsistent_reports_fail_closed(bad):
    with pytest.raises(GroupInsightsDataError):
        summarize_group_summons(bad, NOW)


async def test_snapshot_query_is_group_and_actor_scoped_and_bounded():
    session = object()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    client = SimpleNamespace(start_session=MagicMock(return_value=context))
    cursor = SimpleNamespace(to_list=AsyncMock(return_value=[row()]))
    spawns = SimpleNamespace(aggregate=AsyncMock(return_value=cursor))
    result = await GroupInsightsRepository(client, SimpleNamespace(spawns=spawns)).read(
        -100, 42, NOW
    )
    assert result["my_catches"] == 2
    client.start_session.assert_called_once_with(snapshot=True, causal_consistency=False)
    call = spawns.aggregate.await_args
    assert call.args[0][0] == {
        "$match": {"group_id": -100, "created_at": {"$gte": NOW - timedelta(days=30), "$lte": NOW}}
    }
    assert call.kwargs["session"] is session
    assert call.kwargs["allowDiskUse"] is False and call.kwargs["maxTimeMS"] == 4000
    assert '"$claimed_by", 42' in str(call.args[0]).replace("'", '"')
    assert "name" not in str(call.args[0])


@pytest.mark.parametrize(
    "group,user,when",
    [
        (0, 42, NOW),
        (1, 42, NOW),
        (True, 42, NOW),
        (-100, 0, NOW),
        (-100, True, NOW),
        (-100, 2**53, NOW),
        (-100, 42, NOW.replace(tzinfo=None)),
    ],
)
async def test_invalid_scope_rejected_before_any_query(group, user, when):
    client = SimpleNamespace(start_session=MagicMock())
    with pytest.raises(ValueError):
        await GroupInsightsRepository(client, object()).read(group, user, when)
    client.start_session.assert_not_called()
