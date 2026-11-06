"""Permission, scoping and transaction boundaries for warning corrections."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetChatMember
from fastapi.testclient import TestClient
from pymongo import DESCENDING, ReturnDocument
from pymongo.errors import AutoReconnect

from summon.api.app import create_app
from summon.auth import SessionCodec, TelegramUser
from summon.config import Settings
from summon.repositories.groups import GroupRepository
from summon.services.moderation import ModerationService, PermissionDenied

NOW = datetime(2026, 11, 5, tzinfo=UTC)
WARNING_ID = "a" * 32
NEXT_ID = "b" * 32


def role(status="administrator", restrict=True):
    return SimpleNamespace(status=status, can_restrict_members=restrict)


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sort_keys = None
        self.maximum = None
        self.deadline_ms = None

    def sort(self, keys):
        self.sort_keys = keys
        return self

    def limit(self, maximum):
        self.maximum = maximum
        return self

    def max_time_ms(self, deadline):
        self.deadline_ms = deadline
        return self

    async def to_list(self, length):
        return self.rows[:length]


async def direct_transaction(_client, callback):
    return await callback("txn")


async def test_issue_counts_only_active_warnings_including_legacy_absent_field(monkeypatch):
    monkeypatch.setattr("summon.repositories.groups.transaction", direct_transaction)
    warnings = SimpleNamespace(insert_one=AsyncMock(), count_documents=AsyncMock(return_value=2))
    activity = SimpleNamespace(insert_one=AsyncMock())
    repo = GroupRepository(object(), SimpleNamespace(warnings=warnings, activity=activity))
    result = await repo.record_moderation(-100, 42, 55, "warn", "A reason", NOW)
    assert result["warning_count"] == 2
    assert warnings.count_documents.await_args.args[0] == {
        "group_id": -100,
        "user_id": 55,
        "revoked_at": None,
    }
    assert warnings.count_documents.await_args.kwargs["session"] == "txn"
    assert warnings.insert_one.await_args.kwargs["session"] == "txn"
    assert activity.insert_one.await_args.kwargs["session"] == "txn"
    assert (
        activity.insert_one.await_args.args[0]["warning_id"]
        == warnings.insert_one.await_args.args[0]["_id"]
    )


async def test_revoke_is_atomic_scoped_and_audited_in_same_transaction(monkeypatch):
    monkeypatch.setattr("summon.repositories.groups.transaction", direct_transaction)
    warnings = SimpleNamespace(find_one_and_update=AsyncMock(return_value={"_id": WARNING_ID}))
    activity = SimpleNamespace(insert_one=AsyncMock())
    repo = GroupRepository(object(), SimpleNamespace(warnings=warnings, activity=activity))
    result = await repo.revoke_warning(-100, 42, 55, WARNING_ID, "Correction", NOW)
    query, mutation = warnings.find_one_and_update.await_args.args
    assert query == {"_id": WARNING_ID, "group_id": -100, "user_id": 55, "revoked_at": None}
    assert mutation == {
        "$set": {"revoked_at": NOW, "revoked_by": 42, "revocation_reason": "Correction"}
    }
    assert warnings.find_one_and_update.await_args.kwargs == {
        "session": "txn",
        "return_document": ReturnDocument.AFTER,
    }
    event = activity.insert_one.await_args.args[0]
    assert {
        key: event[key]
        for key in (
            "group_id",
            "user_id",
            "target_id",
            "kind",
            "action",
            "warning_id",
            "reason",
            "created_at",
        )
    } == {
        "group_id": -100,
        "user_id": 42,
        "target_id": 55,
        "kind": "moderation",
        "action": "warning_revoked",
        "warning_id": WARNING_ID,
        "reason": "Correction",
        "created_at": NOW,
    }
    assert activity.insert_one.await_args.kwargs["session"] == "txn"
    assert result == {"id": WARNING_ID, "status": "revoked"}


async def test_missing_cross_target_or_already_revoked_warning_never_writes_audit(monkeypatch):
    monkeypatch.setattr("summon.repositories.groups.transaction", direct_transaction)
    warnings = SimpleNamespace(find_one_and_update=AsyncMock(return_value=None))
    activity = SimpleNamespace(insert_one=AsyncMock())
    repo = GroupRepository(object(), SimpleNamespace(warnings=warnings, activity=activity))
    with pytest.raises(LookupError):
        await repo.revoke_warning(-101, 42, 56, WARNING_ID, "", NOW)
    assert warnings.find_one_and_update.await_args.args[0] == {
        "_id": WARNING_ID,
        "group_id": -101,
        "user_id": 56,
        "revoked_at": None,
    }
    activity.insert_one.assert_not_awaited()


async def test_audit_failure_propagates_and_does_not_report_success(monkeypatch):
    monkeypatch.setattr("summon.repositories.groups.transaction", direct_transaction)
    warnings = SimpleNamespace(find_one_and_update=AsyncMock(return_value={"_id": WARNING_ID}))
    activity = SimpleNamespace(insert_one=AsyncMock(side_effect=AutoReconnect("unknown")))
    repo = GroupRepository(object(), SimpleNamespace(warnings=warnings, activity=activity))
    with pytest.raises(AutoReconnect):
        await repo.revoke_warning(-100, 42, 55, WARNING_ID, "", NOW)
    warnings.find_one_and_update.assert_awaited_once()
    activity.insert_one.assert_awaited_once()


async def test_warning_pages_are_bounded_and_cursor_is_scoped_to_target():
    rows = [
        {"_id": WARNING_ID, "actor_id": 42, "reason": "first", "created_at": NOW},
        {
            "_id": NEXT_ID,
            "actor_id": 43,
            "reason": "second",
            "created_at": NOW - timedelta(seconds=1),
        },
    ]
    cursor = FakeCursor(rows)
    warnings = SimpleNamespace(
        find=lambda query: queries.append(query) or cursor,
        find_one=AsyncMock(return_value={"created_at": NOW + timedelta(seconds=1)}),
        count_documents=AsyncMock(return_value=9),
    )
    queries = []
    repo = GroupRepository(object(), SimpleNamespace(warnings=warnings))
    result = await repo.list_warnings(-100, 55, before="c" * 32, limit=1)
    assert warnings.find_one.await_args.args[0] == {
        "_id": "c" * 32,
        "group_id": -100,
        "user_id": 55,
    }
    assert queries == [
        {
            "group_id": -100,
            "user_id": 55,
            "revoked_at": None,
            "$or": [
                {"created_at": {"$lt": NOW + timedelta(seconds=1)}},
                {"created_at": NOW + timedelta(seconds=1), "_id": {"$lt": "c" * 32}},
            ],
        }
    ]
    assert cursor.sort_keys == [("created_at", DESCENDING), ("_id", DESCENDING)]
    assert cursor.maximum == 2
    assert cursor.deadline_ms == 5000
    assert warnings.find_one.await_args.kwargs == {"max_time_ms": 5000}
    assert result == {
        "items": [{"id": WARNING_ID, "actor_id": 42, "reason": "first", "created_at": NOW}],
        "active_count": 9,
        "next_cursor": WARNING_ID,
    }
    warnings.count_documents.assert_awaited_once_with(
        {"group_id": -100, "user_id": 55, "revoked_at": None}, maxTimeMS=5000
    )
    warnings.find_one.return_value = None
    with pytest.raises(LookupError):
        await repo.list_warnings(-101, 55, before="c" * 32, limit=1)
    assert len(queries) == 1


async def test_review_and_revoke_recheck_only_actor_restrict_right_every_time():
    bot = SimpleNamespace(id=99, get_chat_member=AsyncMock(return_value=role()))
    groups = SimpleNamespace(
        list_warnings=AsyncMock(return_value={"items": [], "active_count": 0, "next_cursor": None}),
        revoke_warning=AsyncMock(return_value={"id": WARNING_ID, "status": "revoked"}),
    )
    service = ModerationService(bot, groups)
    await service.list_warnings(-100, 42, 55, limit=5)
    await service.revoke_warning(-100, 42, 55, WARNING_ID, "reason", NOW)
    assert bot.get_chat_member.await_args_list[0].args == (-100, 42)
    assert bot.get_chat_member.await_args_list[1].args == (-100, 42)
    assert bot.get_chat_member.await_count == 2  # correction is not a Telegram restriction
    assert groups.list_warnings.await_args.args == (-100, 55)
    assert groups.revoke_warning.await_args.args == (-100, 42, 55, WARNING_ID, "reason", NOW)
    bot.get_chat_member.return_value = role(restrict=False)
    with pytest.raises(PermissionDenied):
        await service.list_warnings(-100, 42, 55)
    with pytest.raises(PermissionDenied):
        await service.revoke_warning(-100, 42, 55, WARNING_ID, "", NOW)
    assert groups.list_warnings.await_count == 1
    assert groups.revoke_warning.await_count == 1


@pytest.mark.parametrize("target", [0, -1, 2**53, True])
async def test_review_rejects_invalid_target_before_storage(target):
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=role()))
    groups = SimpleNamespace(list_warnings=AsyncMock(), revoke_warning=AsyncMock())
    with pytest.raises(ValueError):
        await ModerationService(bot, groups).list_warnings(-100, 42, target)
    groups.list_warnings.assert_not_awaited()


@pytest.mark.parametrize("bad_id", ["x" * 32, "a" * 31, "A" * 32, "a" * 33])
async def test_revoke_rejects_bad_id_before_storage(bad_id):
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=role()))
    groups = SimpleNamespace(revoke_warning=AsyncMock())
    with pytest.raises(ValueError):
        await ModerationService(bot, groups).revoke_warning(-100, 42, 55, bad_id, "", NOW)
    groups.revoke_warning.assert_not_awaited()


def api_fixture():
    groups = SimpleNamespace(
        list_warnings=AsyncMock(return_value={"items": [], "active_count": 0, "next_cursor": None}),
        revoke_warning=AsyncMock(return_value={"id": WARNING_ID, "status": "revoked"}),
        get=AsyncMock(return_value={"_id": -100, "title": "Test", "settings": {}}),
        link_admin=AsyncMock(),
        recent_activity=AsyncMock(return_value=[]),
    )
    store = SimpleNamespace(
        groups=groups,
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
        sessions=SimpleNamespace(is_active=AsyncMock(return_value=True)),
    )
    bot = SimpleNamespace(id=99, get_chat_member=AsyncMock(return_value=role()))
    secret = "s" * 40
    settings = Settings(session_secret=secret, _env_file=None)
    app = create_app(settings=settings, store=store, telegram_bot=bot)
    return app, store, bot, secret


def attach_session(client, secret):
    digest = "f" * 64
    token = SessionCodec(secret).issue(
        TelegramUser(id=123, first_name="Tester"), launch_digest=digest, session_id="a" * 43
    )
    client.cookies.set("summon_session", token)
    client.headers["X-Summon-Launch"] = digest


def test_api_review_and_revoke_use_session_actor_live_rights_and_csrf():
    app, store, bot, secret = api_fixture()
    path = "/api/groups/-100/members/55/warnings"
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        attach_session(client, secret)
        res = client.get(f"{path}?limit=5&actor_id=999")
        assert res.status_code == 200
        assert res.headers["cache-control"] == "no-store"
        assert bot.get_chat_member.await_args.args == (-100, 123)
        assert store.groups.list_warnings.await_args.args == (-100, 55)
        no_csrf = client.post(f"{path}/{WARNING_ID}/revoke", json={"reason": "fixed"})
        assert no_csrf.status_code == 403
        cross_origin = client.post(
            f"{path}/{WARNING_ID}/revoke",
            json={"reason": "fixed"},
            headers={"X-Summon-Request": "1", "Origin": "https://evil.example"},
        )
        assert cross_origin.status_code == 403
        spoofed = client.post(
            f"{path}/{WARNING_ID}/revoke",
            json={"reason": "fixed", "actor_id": 999},
            headers={"X-Summon-Request": "1"},
        )
        assert spoofed.status_code == 422
        store.groups.revoke_warning.assert_not_awaited()
        res = client.post(
            f"{path}/{WARNING_ID}/revoke",
            json={"reason": "fixed"},
            headers={"X-Summon-Request": "1"},
        )
        assert res.status_code == 200
        assert store.groups.revoke_warning.await_args.args[:5] == (
            -100,
            123,
            55,
            WARNING_ID,
            "fixed",
        )
        bot.get_chat_member.return_value = role(restrict=False)
        assert client.get(path).status_code == 403
        assert (
            client.post(
                f"{path}/{WARNING_ID}/revoke", json={}, headers={"X-Summon-Request": "1"}
            ).status_code
            == 403
        )
        assert store.groups.list_warnings.await_count == 1
        assert store.groups.revoke_warning.await_count == 1


def test_api_validation_rate_limit_and_not_found_fail_closed():
    app, store, bot, secret = api_fixture()
    path = "/api/groups/-100/members/55/warnings"
    with TestClient(app) as client:
        attach_session(client, secret)
        assert client.get(f"{path}?limit=26").status_code == 422
        assert client.get(f"{path}?before=not-a-cursor").status_code == 422
        assert client.get("/api/groups/-100/members/0/warnings").status_code == 422
        assert client.get("/api/groups/1/members/55/warnings").status_code == 422
        assert client.get(f"/api/groups/-{2**53}/members/55/warnings").status_code == 422
        assert (
            client.post(
                f"{path}/invalid/revoke", json={}, headers={"X-Summon-Request": "1"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"{path}/{WARNING_ID}/revoke",
                json={"reason": "x" * 501},
                headers={"X-Summon-Request": "1"},
            ).status_code
            == 422
        )
        store.groups.revoke_warning.side_effect = LookupError("not found")
        assert (
            client.post(
                f"{path}/{WARNING_ID}/revoke", json={}, headers={"X-Summon-Request": "1"}
            ).status_code
            == 404
        )
        store.limiter.allow.return_value = False
        assert client.get(path).status_code == 429
        assert (
            client.post(
                f"{path}/{WARNING_ID}/revoke", json={}, headers={"X-Summon-Request": "1"}
            ).status_code
            == 429
        )
        assert store.groups.revoke_warning.await_count == 1
        assert store.groups.list_warnings.await_count == 0


def test_api_telegram_and_database_outages_fail_closed_without_retry():
    app, store, bot, secret = api_fixture()
    path = "/api/groups/-100/members/55/warnings"
    with TestClient(app) as client:
        attach_session(client, secret)
        bot.get_chat_member.side_effect = TelegramNetworkError(
            method=GetChatMember(chat_id=-100, user_id=123), message="sensitive failure"
        )
        res = client.get(path)
        assert res.status_code == 503 and "sensitive failure" not in res.text
        store.groups.list_warnings.assert_not_awaited()
        bot.get_chat_member.side_effect = None
        store.groups.revoke_warning.side_effect = AutoReconnect("sensitive connection info")
        res = client.post(f"{path}/{WARNING_ID}/revoke", json={}, headers={"X-Summon-Request": "1"})
        assert res.status_code == 503 and "sensitive connection info" not in res.text
        assert "review warnings before retrying" in res.text
        store.groups.revoke_warning.assert_awaited_once()


def test_nonrestrict_admin_cannot_read_warning_details_via_recent_activity():
    app, store, bot, secret = api_fixture()
    bot.get_chat_member.return_value = role(restrict=False)
    store.groups.recent_activity.return_value = [
        {
            "kind": "moderation",
            "action": "warn",
            "reason": "private reason",
            "target_id": 55,
            "created_at": NOW,
        },
        {"kind": "settings_changed", "action": None, "reason": None, "created_at": NOW},
    ]
    with TestClient(app) as client:
        attach_session(client, secret)
        result = client.get("/api/groups/-100")
        assert result.status_code == 200
        assert "private reason" not in result.text
        assert [entry["kind"] for entry in result.json()["data"]["recent_activity"]] == [
            "settings_changed"
        ]


def test_preview_returns_only_explicit_sample_and_rejects_mutations():
    app = create_app(settings=Settings(preview_mode=True, _env_file=None))
    with TestClient(app) as client:
        path = "/api/groups/-100/members/456/warnings"
        sample = client.get(path)
        assert sample.status_code == 200
        assert sample.json()["data"]["active_count"] == 1
        assert "sample" in sample.text.lower()
        assert client.get("/api/groups/-101/members/456/warnings").status_code == 404
        assert (
            client.get("/api/groups/-100/members/457/warnings").json()["data"]["active_count"] == 0
        )
        assert (
            client.post(
                f"{path}/{WARNING_ID}/revoke", json={}, headers={"X-Summon-Request": "1"}
            ).status_code
            == 403
        )


async def test_fake_concurrent_revocations_only_one_wins_and_writes_audit(monkeypatch):
    import asyncio

    monkeypatch.setattr("summon.repositories.groups.transaction", direct_transaction)

    class AtomicWarnings:
        def __init__(self):
            self.row = {"_id": WARNING_ID, "group_id": -100, "user_id": 55}
            self.lock = asyncio.Lock()

        async def find_one_and_update(self, query, update, **kwargs):
            async with self.lock:
                if (
                    self.row.get("_id") != query["_id"]
                    or self.row.get("group_id") != query["group_id"]
                    or self.row.get("user_id") != query["user_id"]
                    or self.row.get("revoked_at") is not None
                ):
                    return None
                self.row.update(update["$set"])
                return dict(self.row)

    warnings = AtomicWarnings()
    activity = SimpleNamespace(insert_one=AsyncMock())
    repo = GroupRepository(object(), SimpleNamespace(warnings=warnings, activity=activity))
    results = await asyncio.gather(
        repo.revoke_warning(-100, 42, 55, WARNING_ID, "one", NOW),
        repo.revoke_warning(-100, 43, 55, WARNING_ID, "two", NOW),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, LookupError) for result in results) == 1
    assert activity.insert_one.await_count == 1
    assert warnings.row["revoked_by"] in {42, 43}


async def test_warning_issuance_never_applies_automatic_restriction():
    bot = SimpleNamespace(
        id=99,
        get_chat_member=AsyncMock(side_effect=[role("creator"), role(), role("member")]),
        ban_chat_member=AsyncMock(),
        restrict_chat_member=AsyncMock(),
        unban_chat_member=AsyncMock(),
    )
    groups = SimpleNamespace(
        record_moderation=AsyncMock(return_value={"action": "warn", "warning_count": 20})
    )
    result = await ModerationService(bot, groups).act(-100, 42, 55, "warn", "", NOW)
    assert result["warning_count"] == 20
    bot.ban_chat_member.assert_not_awaited()
    bot.restrict_chat_member.assert_not_awaited()
    bot.unban_chat_member.assert_not_awaited()


async def test_recent_activity_includes_warning_reference_for_authorized_callers():
    class Cursor:
        def sort(self, *_args):
            return self

        def limit(self, _limit):
            return self

        def __aiter__(self):
            async def rows():
                yield {
                    "kind": "moderation",
                    "action": "warning_revoked",
                    "warning_id": WARNING_ID,
                    "user_id": 42,
                    "target_id": 55,
                    "reason": "corrected",
                    "created_at": NOW,
                }

            return rows()

    repo = GroupRepository(
        object(), SimpleNamespace(activity=SimpleNamespace(find=lambda _query: Cursor()))
    )
    result = await repo.recent_activity(-100)
    assert result[0]["warning_id"] == WARNING_ID
    assert result[0]["target_id"] == 55


@pytest.mark.parametrize("limit", [0, 26, True, -1])
async def test_service_rejects_unbounded_pages(limit):
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=role()))
    groups = SimpleNamespace(list_warnings=AsyncMock())
    with pytest.raises(ValueError):
        await ModerationService(bot, groups).list_warnings(-100, 42, 55, limit=limit)
    groups.list_warnings.assert_not_awaited()


async def test_warning_reason_is_bounded_before_revoke():
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=role()))
    groups = SimpleNamespace(revoke_warning=AsyncMock())
    with pytest.raises(ValueError):
        await ModerationService(bot, groups).revoke_warning(
            -100, 42, 55, WARNING_ID, "x" * 501, NOW
        )
    groups.revoke_warning.assert_not_awaited()


def test_warning_pagination_index_is_added_without_removing_old_index():
    from pymongo import ASCENDING, DESCENDING

    from summon.database import INDEXES

    keys = [index[0] for index in INDEXES["warnings"]]
    assert [("group_id", ASCENDING), ("user_id", ASCENDING), ("created_at", DESCENDING)] in keys
    assert [
        ("group_id", ASCENDING),
        ("user_id", ASCENDING),
        ("revoked_at", ASCENDING),
        ("created_at", DESCENDING),
        ("_id", DESCENDING),
    ] in keys


def test_default_api_entrypoint_does_not_log_sensitive_request_paths():
    from unittest.mock import patch

    from summon.api.run import main

    with patch("summon.api.run.uvicorn.run") as run:
        main()
    assert run.call_args.kwargs["access_log"] is False


def test_warning_requests_log_route_templates_without_ids_or_reasons(caplog):
    import logging

    app, _, _, secret = api_fixture()
    with TestClient(app) as client, caplog.at_level(logging.INFO, logger="summon.api.app"):
        attach_session(client, secret)
        result = client.post(
            f"/api/groups/-100/members/55/warnings/{WARNING_ID}/revoke",
            json={"reason": "private correction"},
            headers={"X-Summon-Request": "1"},
        )
        assert result.status_code == 200
    log = "\n".join(
        record.getMessage() for record in caplog.records if record.name == "summon.api.app"
    )
    assert "route=" in log
    assert "private correction" not in log
    assert WARNING_ID not in log
    assert "/members/55/" not in log
