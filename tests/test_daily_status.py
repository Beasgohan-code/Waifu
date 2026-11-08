from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from summon.api.app import create_app
from summon.auth import TelegramUser
from summon.config import Settings
from summon.repositories.users import DailyAlreadyClaimed, DailyStatusUnavailable, UserRepository
from summon.repositories.users import daily_status as status_for


def test_daily_status_uses_utc_not_the_timestamp_offset():
    india = timezone(timedelta(hours=5, minutes=30))
    before = datetime(2026, 1, 2, 5, 29, 59, tzinfo=india)
    claimed = status_for("2026-01-01", before)
    assert claimed == {
        "state": "claimed",
        "server_time": "2026-01-01T23:59:59+00:00",
        "next_reset_at": "2026-01-02T00:00:00+00:00",
    }
    available = status_for("2026-01-01", before + timedelta(seconds=1))
    assert available["state"] == "available"
    assert available["server_time"] == "2026-01-02T00:00:00+00:00"
    assert available["next_reset_at"] == "2026-01-03T00:00:00+00:00"
    assert status_for(None, before)["state"] == "available"


@pytest.mark.parametrize("last", ["not-a-date", "2026-99-99", "20260101", "2027-01-01", 9])
def test_invalid_or_future_daily_date_fails_closed(last):
    assert status_for(last, datetime(2026, 1, 1, tzinfo=UTC))["state"] == "unavailable"


def test_daily_status_rejects_naive_timestamps():
    with pytest.raises(ValueError, match="timezone-aware"):
        status_for(None, datetime(2026, 1, 1))


async def test_progression_uses_utc_day_and_bounded_catch_window():
    now = datetime(2026, 1, 2, 4, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    activity = SimpleNamespace(find_one=AsyncMock(return_value=None))
    repo = UserRepository(object(), SimpleNamespace(activity=activity))
    repo.profile = AsyncMock(
        return_value={
            "catch_count": 0,
            "unique_characters": 0,
            "streak": 1,
            "level": 1,
            "last_daily": "2026-01-01",
        }
    )
    progression = await repo.progression(123, now)
    assert progression["daily_status"]["state"] == "claimed"
    assert progression["daily"][0]["completed"] is True
    query = activity.find_one.await_args.args[0]
    assert query["created_at"] == {
        "$gte": datetime(2026, 1, 1, tzinfo=UTC),
        "$lt": datetime(2026, 1, 2, tzinfo=UTC),
    }


@pytest.mark.parametrize(
    ("last", "error"),
    [
        ("2026-01-02", DailyAlreadyClaimed),
        ("2026-01-03", DailyStatusUnavailable),
        ("broken", DailyStatusUnavailable),
    ],
)
async def test_daily_rejects_claimed_future_and_corrupt_dates_without_granting(last, error):
    users = SimpleNamespace(
        find_one=AsyncMock(return_value={"last_daily": last}), update_one=AsyncMock()
    )
    history = SimpleNamespace(insert_one=AsyncMock())
    repo = UserRepository(object(), SimpleNamespace(users=users, reward_history=history))
    repo.touch = AsyncMock()

    async def run_transaction(_client, callback):
        return await callback(object())

    with patch("summon.repositories.users.transaction", side_effect=run_transaction):
        with pytest.raises(error):
            await repo.daily(
                TelegramUser(id=123, first_name="Test"), datetime(2026, 1, 2, tzinfo=UTC)
            )
    users.update_one.assert_not_awaited()
    history.insert_one.assert_not_awaited()


def test_preview_exposes_server_status_but_stays_read_only():
    with TestClient(create_app(settings=Settings(preview_mode=True, _env_file=None))) as client:
        status = client.get("/api/progression").json()["data"]["daily_status"]
        assert status["state"] == "available"
        assert datetime.fromisoformat(status["server_time"]).utcoffset() == timedelta(0)
        assert client.post("/api/daily", headers={"X-Summon-Request": "1"}).status_code == 403
