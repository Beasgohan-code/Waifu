"""Public exact-match replies: bounded storage, revisions and fresh admin rights."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetChatMember
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from summon.api.app import create_app
from summon.auth import SessionCodec, TelegramUser
from summon.auto_replies import (
    AutoReplyConflict,
    AutoReplyLimitError,
    AutoReplyStorageError,
    find_auto_reply,
    normalize_trigger,
    validate_reply_text,
)
from summon.config import Settings
from summon.repositories.auto_replies import AutoReplyRepository
from summon.services.auto_replies import AutoReplyService
from summon.services.moderation import PermissionDenied

NOW = datetime(2026, 11, 6, tzinfo=UTC)
REV = "a" * 32


def stored(trigger="hello", response="Public reply", *, enabled=True, revision=REV):
    return {
        "trigger": trigger,
        "response": response,
        "enabled": enabled,
        "revision": revision,
        "created_at": NOW,
        "created_by": 42,
        "updated_at": NOW,
        "updated_by": 42,
    }


async def direct_transaction(_client, callback):
    return await callback("txn")


class FakeGroups:
    def __init__(self):
        self.documents = {-100: {"_id": -100}, -200: {"_id": -200}}
        self.reads = []
        self.writes = []

    async def find_one(self, query, projection=None, **kwargs):
        self.reads.append((query, projection, kwargs))
        return deepcopy(self.documents.get(query["_id"]))

    async def update_one(self, query, change, **kwargs):
        self.writes.append((query, change, kwargs))
        document = self.documents.get(query["_id"])
        if document is None:
            return SimpleNamespace(matched_count=0)
        for path, value in change.get("$set", {}).items():
            field, name = path.split(".")
            document.setdefault(field, {})[name] = deepcopy(value)
        for path in change.get("$unset", {}):
            field, name = path.split(".")
            document.get(field, {}).pop(name, None)
        return SimpleNamespace(matched_count=1)


@pytest.fixture
def repository(monkeypatch):
    monkeypatch.setattr("summon.repositories.auto_replies.transaction", direct_transaction)
    groups = FakeGroups()
    activity = SimpleNamespace(insert_one=AsyncMock())
    return (
        AutoReplyRepository(object(), SimpleNamespace(groups=groups, activity=activity)),
        groups,
        activity,
    )


def test_trigger_normalization_is_unicode_aware_exact_and_deterministic():
    assert normalize_trigger("  Ｈｅｌｌｏ   Straße  ") == "hello strasse"
    group = {"auto_replies_v1": {"greet": stored(trigger="hello strasse")}}
    assert find_auto_reply(group, "  Hello STRASSE ") == "Public reply"
    assert find_auto_reply(group, "say hello strasse") is None
    assert find_auto_reply(group, "hello strasse please") is None
    group["auto_replies_v1"]["greet"]["enabled"] = False
    assert find_auto_reply(group, "hello strasse") is None
    assert find_auto_reply({}, "hello") is None


@pytest.mark.parametrize(
    "value", ["", " ", "a", "/help", "  /help  ", "x" * 65, "abc\nxyz", "ab\x00c", "\ud800"]
)
def test_invalid_triggers_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_trigger(value)
    assert find_auto_reply({}, value) is None


@pytest.mark.parametrize("value", ["", "   ", "x" * 801, "bad\x00text", "\ud800"])
def test_invalid_public_reply_text_is_rejected(value):
    with pytest.raises(ValueError):
        validate_reply_text(value)


def test_public_reply_text_accepts_unicode_and_literal_markup():
    validate_reply_text("<b>Not markup</b> & welcome\n😀")
    validate_reply_text("😀" * 800)


async def test_create_edit_delete_are_scoped_revision_checked_and_audited_without_content(
    repository,
):
    repo, groups, activity = repository
    first = await repo.save(
        -100, 42, "greeting", " Ｈello  World ", "<hello & welcome>", True, None, NOW
    )
    assert first["name"] == "greeting" and first["trigger"] == "hello world"
    assert first["response"] == "<hello & welcome>" and first["enabled"] is True
    assert len(first["revision"]) == 32
    assert groups.writes[0][0] == {"_id": -100} and groups.writes[0][2]["session"] == "txn"
    event = activity.insert_one.await_args.args[0]
    assert {k: event[k] for k in ("group_id", "user_id", "kind", "action", "reply_name")} == {
        "group_id": -100,
        "user_id": 42,
        "kind": "auto_reply_changed",
        "action": "created",
        "reply_name": "greeting",
    }
    assert "hello" not in repr(event).lower() and "response" not in event and "trigger" not in event
    assert activity.insert_one.await_args.kwargs["session"] == "txn"
    assert await repo.list(-200) == []
    assert groups.reads[-1][2]["max_time_ms"] == 5000

    updated = await repo.save(
        -100, 43, "greeting", "hello world", "New text", False, first["revision"], NOW
    )
    assert updated["revision"] != first["revision"]
    assert updated["created_by"] == 42 and updated["updated_by"] == 43 and not updated["enabled"]
    with pytest.raises(AutoReplyConflict):
        await repo.save(-100, 42, "greeting", "hello world", "stale", True, first["revision"], NOW)
    with pytest.raises(AutoReplyConflict):
        await repo.delete(-100, 42, "greeting", first["revision"], NOW)
    with pytest.raises(AutoReplyConflict):
        await repo.delete(-200, 42, "greeting", updated["revision"], NOW)
    assert activity.insert_one.await_count == 2
    assert await repo.delete(-100, 42, "greeting", updated["revision"], NOW) == {
        "name": "greeting",
        "status": "deleted",
    }
    assert activity.insert_one.await_args.args[0]["action"] == "deleted"
    assert await repo.list(-100) == []


async def test_duplicate_normalized_triggers_and_duplicate_names_never_overwrite(repository):
    repo, groups, activity = repository
    groups.documents[-100]["auto_replies_v1"] = {"one": stored(trigger="hello strasse")}
    with pytest.raises(AutoReplyConflict):
        await repo.save(-100, 42, "other", "HELLO Straße", "text", False, None, NOW)
    with pytest.raises(AutoReplyConflict):
        await repo.save(-100, 42, "one", "different", "text", False, None, NOW)
    with pytest.raises(AutoReplyConflict):
        await repo.save(-100, 42, "missing", "different", "text", False, REV, NOW)
    with pytest.raises(LookupError):
        await repo.save(-999, 42, "new", "different", "text", False, None, NOW)
    assert not groups.writes
    activity.insert_one.assert_not_awaited()


async def test_cap_is_enforced_but_existing_rules_can_be_edited(repository):
    repo, groups, _ = repository
    groups.documents[-100]["auto_replies_v1"] = {
        f"r{i}": stored(trigger=f"trigger {i}") for i in range(20)
    }
    with pytest.raises(AutoReplyLimitError):
        await repo.save(-100, 42, "new", "new trigger", "text", False, None, NOW)
    updated = await repo.save(-100, 42, "r0", "trigger 0", "Updated", False, REV, NOW)
    assert updated["response"] == "Updated" and len(await repo.list(-100)) == 20


@pytest.mark.parametrize(
    "corrupt",
    [
        [],
        {".bad": stored()},
        {"x": stored(enabled="yes")},
        {"x": stored(trigger="HELLO")},
        {"x": stored(), "y": stored()},
    ],
)
async def test_corrupt_existing_storage_fails_closed_without_overwriting(repository, corrupt):
    repo, groups, activity = repository
    groups.documents[-100]["auto_replies_v1"] = corrupt
    with pytest.raises(AutoReplyStorageError):
        await repo.list(-100)
    with pytest.raises(AutoReplyStorageError):
        await repo.save(-100, 42, "new", "new trigger", "text", False, None, NOW)
    with pytest.raises(AutoReplyStorageError):
        find_auto_reply(groups.documents[-100], "hello")
    assert groups.documents[-100]["auto_replies_v1"] == corrupt
    assert not groups.writes
    activity.insert_one.assert_not_awaited()


async def test_transaction_retry_reuses_revision_and_audit_identifier(repository, monkeypatch):
    repo, groups, activity = repository
    before = deepcopy(groups.documents)
    calls = []

    async def retry_transaction(_client, callback):
        first = await callback("aborted")
        calls.append((first["revision"], activity.insert_one.await_args.args[0]["_id"]))
        groups.documents = deepcopy(before)
        second = await callback("committed")
        calls.append((second["revision"], activity.insert_one.await_args.args[0]["_id"]))
        return second

    monkeypatch.setattr("summon.repositories.auto_replies.transaction", retry_transaction)
    result = await repo.save(-100, 42, "new", "hello", "text", True, None, NOW)
    assert calls[0] == calls[1] and result["revision"] == calls[1][0]


def member(change=True):
    return SimpleNamespace(status="administrator", can_change_info=change)


async def test_service_checks_current_admin_right_for_every_operation():
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=member()))
    replies = SimpleNamespace(
        list=AsyncMock(return_value=[]),
        save=AsyncMock(return_value={}),
        delete=AsyncMock(return_value={}),
    )
    service = AutoReplyService(bot, replies)
    await service.list_for_admin(-100, 42)
    await service.save(-100, 42, "hi", "hello", "response", True, None, NOW)
    await service.delete(-100, 42, "hi", REV, NOW)
    assert bot.get_chat_member.await_count == 3
    bot.get_chat_member.return_value = member(False)
    for operation in (
        service.list_for_admin(-100, 42),
        service.save(-100, 42, "hi", "hello", "response", True, None, NOW),
        service.delete(-100, 42, "hi", REV, NOW),
    ):
        with pytest.raises(PermissionDenied):
            await operation
    assert replies.list.await_count == replies.save.await_count == replies.delete.await_count == 1


def api_fixture():
    replies = SimpleNamespace(
        list=AsyncMock(return_value=[{"name": "hi", **stored()}]),
        save=AsyncMock(return_value={"name": "hi", **stored()}),
        delete=AsyncMock(return_value={"name": "hi", "status": "deleted"}),
    )
    store = SimpleNamespace(
        auto_replies=replies,
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
        sessions=SimpleNamespace(is_active=AsyncMock(return_value=True)),
    )
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=member()))
    secret = "s" * 40
    return (
        create_app(
            settings=Settings(session_secret=secret, _env_file=None), store=store, telegram_bot=bot
        ),
        store,
        bot,
        secret,
    )


def attach_session(client, secret):
    digest = "f" * 64
    client.cookies.set(
        "summon_session",
        SessionCodec(secret).issue(
            TelegramUser(id=123, first_name="Tester"), launch_digest=digest, session_id="a" * 43
        ),
    )
    client.headers["X-Summon-Launch"] = digest


def test_api_auth_csrf_live_rights_group_scoping_and_validation():
    app, store, bot, secret = api_fixture()
    path = "/api/groups/-100/auto-replies"
    body = {"trigger": "Hello", "response": "Welcome", "enabled": True, "expected_revision": None}
    csrf = {"X-Summon-Request": "1"}
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        attach_session(client, secret)
        assert client.get(path).status_code == 200
        assert bot.get_chat_member.await_args.args == (-100, 123)
        assert store.auto_replies.list.await_args.args == (-100,)
        assert client.put(f"{path}/hi", json=body).status_code == 403
        assert (
            client.put(f"{path}/hi", json={**body, "actor_id": 999}, headers=csrf).status_code
            == 422
        )
        assert (
            client.put(
                f"{path}/hi",
                json={k: v for k, v in body.items() if k != "expected_revision"},
                headers=csrf,
            ).status_code
            == 422
        )
        assert client.put(f"{path}/hi", json=body, headers=csrf).status_code == 200
        assert store.auto_replies.save.await_args.args[:7] == (
            -100,
            123,
            "hi",
            "hello",
            "Welcome",
            True,
            None,
        )
        assert (
            client.post(
                f"{path}/hi/delete", json={"expected_revision": REV}, headers=csrf
            ).status_code
            == 200
        )
        assert store.auto_replies.delete.await_args.args[:4] == (-100, 123, "hi", REV)
        bot.get_chat_member.return_value = member(False)
        assert client.get(path).status_code == 403
        assert client.put(f"{path}/hi", json=body, headers=csrf).status_code == 403
        assert (
            client.post(
                f"{path}/hi/delete", json={"expected_revision": REV}, headers=csrf
            ).status_code
            == 403
        )
        assert store.auto_replies.save.await_count == store.auto_replies.delete.await_count == 1


def test_api_limits_errors_and_uncertain_writes_are_sanitized():
    app, store, bot, secret = api_fixture()
    path = "/api/groups/-100/auto-replies/hi"
    body = {"trigger": "hello", "response": "Welcome", "enabled": True, "expected_revision": None}
    csrf = {"X-Summon-Request": "1"}
    with TestClient(app) as client:
        attach_session(client, secret)
        for bad in ("bad.name", "Upper", "x" * 33):
            assert (
                client.put(
                    f"/api/groups/-100/auto-replies/{bad}", json=body, headers=csrf
                ).status_code
                == 422
            )
        for change in (
            {"trigger": "/help"},
            {"response": " "},
            {"enabled": "true"},
            {"expected_revision": "bad"},
        ):
            assert client.put(path, json={**body, **change}, headers=csrf).status_code == 422
        store.auto_replies.save.side_effect = AutoReplyConflict("Changed; reload")
        assert client.put(path, json=body, headers=csrf).status_code == 409
        store.auto_replies.save.side_effect = AutoReconnect("sensitive database details")
        result = client.put(path, json=body, headers=csrf)
        assert (
            result.status_code == 503
            and "review" in result.text.lower()
            and "sensitive" not in result.text
        )
        store.limiter.allow.return_value = False
        assert client.get("/api/groups/-100/auto-replies").status_code == 429
        store.limiter.allow.return_value = True
        bot.get_chat_member.side_effect = TelegramNetworkError(
            method=GetChatMember(chat_id=-100, user_id=123), message="private network details"
        )
        result = client.get("/api/groups/-100/auto-replies")
        assert result.status_code == 503 and "private network" not in result.text


def test_preview_is_read_only_and_returns_only_synthetic_rules():
    app = create_app(settings=Settings(preview_mode=True, _env_file=None))
    with TestClient(app) as client:
        result = client.get("/api/groups/-100/auto-replies")
        assert result.status_code == 200 and result.json()["data"]
        assert client.get("/api/groups/-200/auto-replies").status_code == 404
        assert (
            client.put(
                "/api/groups/-100/auto-replies/hello",
                json={
                    "trigger": "hi",
                    "response": "hello",
                    "enabled": True,
                    "expected_revision": None,
                },
                headers={"X-Summon-Request": "1"},
            ).status_code
            == 403
        )


def test_database_validator_is_optional_bounded_and_separate_from_public_notes():
    from summon.database import COLLECTION_SCHEMAS

    schema = COLLECTION_SCHEMAS["groups"]
    assert "auto_replies_v1" not in schema["required"]
    rules = schema["properties"]["auto_replies_v1"]
    assert (
        rules["bsonType"] == "object"
        and rules["maxProperties"] == 20
        and rules["additionalProperties"] is False
    )
    entry = rules["patternProperties"]["^[a-z0-9][a-z0-9_-]{0,31}$"]
    assert {
        "trigger",
        "response",
        "enabled",
        "revision",
        "created_at",
        "created_by",
        "updated_at",
        "updated_by",
    } == set(entry["required"])
    assert entry["properties"]["response"]["maxLength"] == 800


async def test_delivery_claim_uses_a_bounded_majority_write_and_hashed_key():
    from pymongo.errors import DuplicateKeyError

    from summon.repositories.limits import RateLimiter

    collection = SimpleNamespace(insert_one=AsyncMock())
    rate_limits = SimpleNamespace(with_options=lambda **_kwargs: collection)
    limiter = RateLimiter(SimpleNamespace(rate_limits=rate_limits))
    assert await limiter.claim_once("group:-100:auto-reply:message:123", seconds=172800)
    document = collection.insert_one.await_args.args[0]
    assert document["_id"].startswith("once:") and "-100" not in document["_id"]
    assert 172790 <= (document["expires_at"] - datetime.now(UTC)).total_seconds() <= 172800
    collection.insert_one.side_effect = DuplicateKeyError("already claimed")
    assert not await limiter.claim_once("group:-100:auto-reply:message:123", seconds=172800)


async def test_delivery_claim_write_concern_is_majority_bounded():
    from pymongo.write_concern import WriteConcern

    from summon.repositories.limits import RateLimiter

    collection = SimpleNamespace(insert_one=AsyncMock())
    options = {}

    def with_options(**kwargs):
        options.update(kwargs)
        return collection

    await RateLimiter(
        SimpleNamespace(rate_limits=SimpleNamespace(with_options=with_options))
    ).claim_once("test", seconds=172800)
    assert options["write_concern"] == WriteConcern(w="majority", wtimeout=5000)


def test_unicode_command_lookalikes_and_invisible_trigger_controls_are_rejected():
    for trigger in ("／help", "hello\u200dworld", "hello\u202eworld", "hello\u007fworld"):
        with pytest.raises(ValueError):
            normalize_trigger(trigger)
    assert normalize_trigger("hello\u00a0WORLD") == "hello world"
    for response in ("bad\u202eformat", "bad\u007fcontrol"):
        with pytest.raises(ValueError):
            validate_reply_text(response)
    validate_reply_text("👩\u200d💻 and Malayalam ക\u200d")


async def test_activity_feed_returns_only_reply_metadata():
    from summon.repositories.groups import GroupRepository

    class Cursor:
        def sort(self, *_args):
            return self

        def limit(self, *_args):
            return self

        def __aiter__(self):
            async def records():
                yield {
                    "kind": "auto_reply_changed",
                    "action": "created",
                    "reply_name": "greeting",
                    "trigger": "private trigger",
                    "response": "private response",
                    "created_at": NOW,
                }

            return records()

    groups = GroupRepository(
        object(), SimpleNamespace(activity=SimpleNamespace(find=lambda *_args: Cursor()))
    )
    events = await groups.recent_activity(-100)
    assert events[0]["reply_name"] == "greeting"
    assert "private trigger" not in repr(events) and "private response" not in repr(events)
