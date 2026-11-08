"""Public group notes: bounded storage, optimistic edits and live authorization."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from summon.api.app import create_app
from summon.auth import SessionCodec, TelegramUser
from summon.config import Settings
from summon.repositories.group_notes import (
    GroupNoteRepository,
    NoteConflict,
    NoteLimitError,
    NoteStorageError,
)
from summon.services.group_notes import GroupNotesService
from summon.services.moderation import PermissionDenied

NOW = datetime(2026, 11, 5, tzinfo=UTC)
REV = "a" * 32


def stored(content="Hello", revision=REV):
    return {
        "content": content,
        "revision": revision,
        "created_at": NOW,
        "created_by": 42,
        "updated_at": NOW,
        "updated_by": 42,
    }


async def direct_transaction(_client, callback):
    return await callback("txn")


class FakeGroups:
    def __init__(self, documents):
        self.documents = deepcopy(documents)
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
    monkeypatch.setattr("summon.repositories.group_notes.transaction", direct_transaction)
    groups = FakeGroups({-100: {"_id": -100}, -200: {"_id": -200}})
    activity = SimpleNamespace(insert_one=AsyncMock())
    return (
        GroupNoteRepository(object(), SimpleNamespace(groups=groups, activity=activity)),
        groups,
        activity,
    )


async def test_create_edit_delete_are_group_scoped_transactional_and_content_free_in_audit(
    repository,
):
    repo, groups, activity = repository
    first = await repo.save(-100, 42, "welcome", "<hello & welcome>", None, NOW)
    assert first["name"] == "welcome" and first["content"] == "<hello & welcome>"
    assert len(first["revision"]) == 32
    assert groups.writes[0][0] == {"_id": -100}
    assert groups.writes[0][2]["session"] == "txn"
    event = activity.insert_one.await_args.args[0]
    assert {key: event[key] for key in ("group_id", "user_id", "kind", "action", "note_name")} == {
        "group_id": -100,
        "user_id": 42,
        "kind": "note_changed",
        "action": "created",
        "note_name": "welcome",
    }
    assert "<hello" not in repr(event) and "content" not in event
    assert activity.insert_one.await_args.kwargs["session"] == "txn"
    assert await repo.list(-200) == []
    with pytest.raises(LookupError):
        await repo.get(-200, "welcome")

    updated = await repo.save(-100, 43, "welcome", "New text", first["revision"], NOW)
    assert updated["revision"] != first["revision"]
    assert updated["created_by"] == 42 and updated["updated_by"] == 43
    assert activity.insert_one.await_args.args[0]["action"] == "updated"
    with pytest.raises(NoteConflict):
        await repo.save(-100, 42, "welcome", "stale", first["revision"], NOW)
    with pytest.raises(NoteConflict):
        await repo.delete(-100, 42, "welcome", first["revision"], NOW)
    assert activity.insert_one.await_count == 2
    result = await repo.delete(-100, 42, "welcome", updated["revision"], NOW)
    assert result == {"name": "welcome", "status": "deleted"}
    assert activity.insert_one.await_args.args[0]["action"] == "deleted"
    assert await repo.list(-100) == []
    assert groups.documents[-100]["saved_notes_v1"] == {}


async def test_duplicate_create_missing_edit_and_wrong_group_do_not_overwrite(repository):
    repo, groups, activity = repository
    groups.documents[-100]["saved_notes_v1"] = {"name": stored()}
    with pytest.raises(NoteConflict):
        await repo.save(-100, 42, "name", "replacement", None, NOW)
    with pytest.raises(NoteConflict):
        await repo.save(-100, 42, "missing", "replacement", REV, NOW)
    with pytest.raises(NoteConflict):
        await repo.delete(-200, 42, "name", REV, NOW)
    with pytest.raises(LookupError):
        await repo.save(-999, 42, "name", "text", None, NOW)
    assert groups.documents[-100]["saved_notes_v1"]["name"]["content"] == "Hello"
    assert not groups.writes
    activity.insert_one.assert_not_awaited()


async def test_cap_is_enforced_without_blocking_edits_and_reads_have_deadline(repository):
    repo, groups, _ = repository
    groups.documents[-100]["saved_notes_v1"] = {f"n{i}": stored() for i in range(30)}
    notes = await repo.list(-100)
    assert len(notes) == 30 and notes == sorted(notes, key=lambda note: note["name"])
    assert groups.reads[-1][2]["max_time_ms"] == 5000
    with pytest.raises(NoteLimitError):
        await repo.save(-100, 42, "new", "text", None, NOW)
    assert (await repo.save(-100, 42, "n1", "updated", REV, NOW))["content"] == "updated"


@pytest.mark.parametrize("corrupt", [None, [], "text", {"bad.name": {}}, {"ok": {"content": "x"}}])
async def test_corrupt_reserved_field_fails_closed_without_overwriting(repository, corrupt):
    repo, groups, activity = repository
    groups.documents[-100]["saved_notes_v1"] = corrupt
    with pytest.raises(NoteStorageError):
        await repo.list(-100)
    with pytest.raises(NoteStorageError):
        await repo.save(-100, 42, "new", "text", None, NOW)
    assert groups.documents[-100]["saved_notes_v1"] == corrupt
    assert not groups.writes
    activity.insert_one.assert_not_awaited()


async def test_audit_failure_is_not_reported_as_success(repository):
    repo, _, activity = repository
    activity.insert_one.side_effect = AutoReconnect("secret database details")
    with pytest.raises(AutoReconnect):
        await repo.save(-100, 42, "name", "text", None, NOW)


def member(status="administrator", *, change=True, is_member=True):
    return SimpleNamespace(
        status={"creator": "owner", "kicked": "banned"}.get(status, status),
        privileges=SimpleNamespace(can_change_info=change),
        is_member=is_member,
    )


async def test_service_rechecks_admin_right_for_every_read_write_and_delete():
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=member()))
    notes = SimpleNamespace(
        list=AsyncMock(return_value=[]),
        get=AsyncMock(return_value={"name": "x", **stored()}),
        save=AsyncMock(return_value={"name": "x", **stored()}),
        delete=AsyncMock(return_value={"status": "deleted"}),
    )
    service = GroupNotesService(bot, notes)
    await service.list_for_admin(-100, 42)
    await service.get_for_admin(-100, 42, "x")
    await service.save(-100, 42, "x", "text", None, NOW)
    await service.delete(-100, 42, "x", REV, NOW)
    assert bot.get_chat_member.await_count == 4
    assert all(call.args == (-100, 42) for call in bot.get_chat_member.await_args_list)
    bot.get_chat_member.return_value = member(change=False)
    for operation in (
        service.list_for_admin(-100, 42),
        service.get_for_admin(-100, 42, "x"),
        service.save(-100, 42, "x", "text", None, NOW),
        service.delete(-100, 42, "x", REV, NOW),
    ):
        with pytest.raises(PermissionDenied):
            await operation
    assert (
        notes.list.await_count
        == notes.get.await_count
        == notes.save.await_count
        == notes.delete.await_count
        == 1
    )


@pytest.mark.parametrize(
    "status,is_member,allowed",
    [
        ("creator", True, True),
        ("administrator", True, True),
        ("member", True, True),
        ("restricted", True, True),
        ("restricted", False, False),
        ("left", False, False),
        ("kicked", False, False),
        ("unknown", True, False),
    ],
)
async def test_public_group_lookup_requires_fresh_actual_membership(status, is_member, allowed):
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=member(status, is_member=is_member))
    )
    notes = SimpleNamespace(
        list=AsyncMock(return_value=[{"name": "x", **stored()}]),
        get=AsyncMock(return_value={"name": "x", **stored()}),
    )
    service = GroupNotesService(bot, notes)
    if allowed:
        assert await service.list_for_member(-100, 42) == ["x"]
        assert await service.get_for_member(-100, 42, "x") == {"name": "x", "content": "Hello"}
        assert bot.get_chat_member.await_count == 2
    else:
        with pytest.raises(PermissionDenied):
            await service.list_for_member(-100, 42)
        with pytest.raises(PermissionDenied):
            await service.get_for_member(-100, 42, "x")
        notes.list.assert_not_awaited()
        notes.get.assert_not_awaited()


@pytest.mark.parametrize("bad_name", ["", ".x", "x.y", "$x", "a/b", "A", "ä", "x\n", "x" * 33])
async def test_invalid_names_never_reach_storage(bad_name):
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=member()))
    notes = SimpleNamespace(save=AsyncMock())
    with pytest.raises(ValueError):
        await GroupNotesService(bot, notes).save(-100, 42, bad_name, "text", None, NOW)
    notes.save.assert_not_awaited()


@pytest.mark.parametrize("content", ["", "   ", "x" * 1201, "null\x00byte", "\ud800"])
async def test_invalid_note_content_never_reaches_storage(content):
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=member()))
    notes = SimpleNamespace(save=AsyncMock())
    with pytest.raises(ValueError):
        await GroupNotesService(bot, notes).save(-100, 42, "x", content, None, NOW)
    notes.save.assert_not_awaited()


@pytest.mark.parametrize("revision", ["bad", "a" * 31, "A" * 32, "a" * 33, 123])
async def test_invalid_revision_never_reaches_storage(revision):
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=member()))
    notes = SimpleNamespace(delete=AsyncMock())
    with pytest.raises(ValueError):
        await GroupNotesService(bot, notes).delete(-100, 42, "x", revision, NOW)
    notes.delete.assert_not_awaited()


def api_fixture():
    notes = SimpleNamespace(
        list=AsyncMock(return_value=[{"name": "hello", **stored()}]),
        save=AsyncMock(return_value={"name": "hello", **stored()}),
        delete=AsyncMock(return_value={"name": "hello", "status": "deleted"}),
    )
    store = SimpleNamespace(
        notes=notes,
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
        sessions=SimpleNamespace(is_active=AsyncMock(return_value=True)),
    )
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=member()))
    secret = "s" * 40
    app = create_app(
        settings=Settings(session_secret=secret, _env_file=None), store=store, telegram_bot=bot
    )
    return app, store, bot, secret


def attach_session(client, secret):
    digest = "f" * 64
    client.cookies.set(
        "summon_session",
        SessionCodec(secret).issue(
            TelegramUser(id=123, first_name="Tester"), launch_digest=digest, session_id="a" * 43
        ),
    )
    client.headers["X-Summon-Launch"] = digest


def test_api_session_live_rights_csrf_and_group_scoping():
    app, store, bot, secret = api_fixture()
    path = "/api/groups/-100/notes"
    csrf = {"X-Summon-Request": "1"}
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        attach_session(client, secret)
        assert client.get(path).status_code == 200
        assert bot.get_chat_member.await_args.args == (-100, 123)
        assert store.notes.list.await_args.args == (-100,)
        assert (
            client.put(
                f"{path}/hello", json={"content": "text", "expected_revision": None}
            ).status_code
            == 403
        )
        assert (
            client.put(
                f"{path}/hello",
                json={"content": "text", "expected_revision": None},
                headers={**csrf, "Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert (
            client.put(
                f"{path}/hello",
                json={"content": "text", "expected_revision": None, "actor_id": 999},
                headers=csrf,
            ).status_code
            == 422
        )
        assert (
            client.put(f"{path}/hello", json={"content": "text"}, headers=csrf).status_code == 422
        )
        assert (
            client.put(
                f"{path}/hello", json={"content": "text", "expected_revision": None}, headers=csrf
            ).status_code
            == 200
        )
        assert store.notes.save.await_args.args[:5] == (-100, 123, "hello", "text", None)
        assert (
            client.post(
                f"{path}/hello/delete", json={"expected_revision": REV}, headers=csrf
            ).status_code
            == 200
        )
        assert store.notes.delete.await_args.args[:4] == (-100, 123, "hello", REV)
        bot.get_chat_member.return_value = member(change=False)
        assert client.get(path).status_code == 403
        assert (
            client.put(
                f"{path}/hello", json={"content": "x", "expected_revision": REV}, headers=csrf
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"{path}/hello/delete", json={"expected_revision": REV}, headers=csrf
            ).status_code
            == 403
        )
        assert store.notes.save.await_count == store.notes.delete.await_count == 1


def test_api_validation_conflict_limits_and_uncertainty_are_sanitized():
    app, store, bot, secret = api_fixture()
    csrf = {"X-Summon-Request": "1"}
    with TestClient(app) as client:
        attach_session(client, secret)
        for path in ("/api/groups/0/notes", f"/api/groups/{-(2**53)}/notes"):
            assert client.get(path).status_code == 422
        for name in ("bad.name", "Upper", "x" * 33):
            assert (
                client.put(
                    f"/api/groups/-100/notes/{name}",
                    json={"content": "x", "expected_revision": None},
                    headers=csrf,
                ).status_code
                == 422
            )
        path = "/api/groups/-100/notes/name"
        for body in (
            {"content": " ", "expected_revision": None},
            {"content": "x" * 1201, "expected_revision": None},
            {"content": "x", "expected_revision": "bad"},
        ):
            assert client.put(path, json=body, headers=csrf).status_code == 422
        assert (
            client.post(
                f"{path}/delete", json={"expected_revision": None}, headers=csrf
            ).status_code
            == 422
        )
        store.notes.save.side_effect = NoteConflict("Changed; reload notes")
        assert (
            client.put(
                path, json={"content": "x", "expected_revision": None}, headers=csrf
            ).status_code
            == 409
        )
        store.notes.save.side_effect = AutoReconnect("sensitive database details")
        result = client.put(path, json={"content": "x", "expected_revision": None}, headers=csrf)
        assert (
            result.status_code == 503
            and "review" in result.text.lower()
            and "sensitive" not in result.text
        )
        store.limiter.allow.return_value = False
        assert client.get("/api/groups/-100/notes").status_code == 429
        store.limiter.allow.return_value = True
        bot.get_chat_member.side_effect = ConnectionError("private network details")
        result = client.get("/api/groups/-100/notes")
        assert result.status_code == 503 and "private network" not in result.text


def test_preview_notes_are_synthetic_and_all_mutations_are_disabled():
    app = create_app(settings=Settings(preview_mode=True, _env_file=None))
    with TestClient(app) as client:
        sample = client.get("/api/groups/-100/notes")
        assert sample.status_code == 200 and sample.json()["data"]
        assert client.get("/api/groups/-200/notes").status_code == 404
        assert (
            client.put(
                "/api/groups/-100/notes/example",
                json={"content": "x", "expected_revision": None},
                headers={"X-Summon-Request": "1"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/groups/-100/notes/example/delete",
                json={"expected_revision": REV},
                headers={"X-Summon-Request": "1"},
            ).status_code
            == 403
        )


def test_group_validator_has_optional_bounded_note_map_without_renaming_existing_fields():
    from summon.database import COLLECTION_SCHEMAS

    group_schema = COLLECTION_SCHEMAS["groups"]
    assert "saved_notes_v1" not in group_schema["required"]
    notes = group_schema["properties"]["saved_notes_v1"]
    assert notes["bsonType"] == "object" and notes["maxProperties"] == 30
    assert notes["additionalProperties"] is False
    entry = notes["patternProperties"]["^[a-z0-9][a-z0-9_-]{0,31}$"]
    assert set(entry["required"]) == {
        "content",
        "revision",
        "created_at",
        "created_by",
        "updated_at",
        "updated_by",
    }
    assert entry["properties"]["content"]["maxLength"] == 1200


async def test_group_activity_exposes_note_name_but_never_note_body():
    from summon.repositories.groups import GroupRepository

    class Cursor:
        def sort(self, *_args):
            return self

        def limit(self, _limit):
            return self

        def __aiter__(self):
            async def rows():
                yield {
                    "kind": "note_changed",
                    "action": "updated",
                    "note_name": "guide",
                    "note_revision": REV,
                    "content": "not for activity",
                    "user_id": 42,
                    "created_at": NOW,
                }

            return rows()

    repo = GroupRepository(
        object(), SimpleNamespace(activity=SimpleNamespace(find=lambda _q: Cursor()))
    )
    result = await repo.recent_activity(-100)
    assert result[0]["note_name"] == "guide"
    assert "not for activity" not in repr(result)


async def test_simulated_transaction_retry_reuses_revision_and_audit_id(repository, monkeypatch):
    repo, groups, activity = repository
    before = deepcopy(groups.documents)
    calls = []

    async def retry_transaction(_client, callback):
        first = await callback("aborted")
        calls.append((first["revision"], activity.insert_one.await_args.args[0]["_id"]))
        groups.documents = deepcopy(before)  # Simulate an aborted snapshot before retry.
        second = await callback("committed")
        calls.append((second["revision"], activity.insert_one.await_args.args[0]["_id"]))
        return second

    monkeypatch.setattr("summon.repositories.group_notes.transaction", retry_transaction)
    result = await repo.save(-100, 42, "guide", "text", None, NOW)
    assert calls[0] == calls[1]
    assert result["revision"] == calls[1][0]


async def test_fake_serialized_race_does_not_duplicate_edits_or_exceed_cap(repository, monkeypatch):
    import asyncio

    repo, groups, activity = repository
    lock = asyncio.Lock()

    async def serialized_transaction(_client, callback):
        async with lock:
            return await callback("txn")

    monkeypatch.setattr("summon.repositories.group_notes.transaction", serialized_transaction)
    groups.documents[-100]["saved_notes_v1"] = {f"n{i}": stored() for i in range(29)}
    results = await asyncio.gather(
        repo.save(-100, 42, "new-a", "First", None, NOW),
        repo.save(-100, 43, "new-b", "Second", None, NOW),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, NoteLimitError) for result in results) == 1
    assert len(await repo.list(-100)) == 30
    assert activity.insert_one.await_count == 1
    # The same revision cannot be accepted twice under this serialized fake.
    results = await asyncio.gather(
        repo.save(-100, 42, "n1", "First", REV, NOW),
        repo.save(-100, 43, "n1", "Second", REV, NOW),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, NoteConflict) for result in results) == 1


def test_api_route_logs_do_not_include_note_name_or_body(caplog):
    import logging

    app, _, _, secret = api_fixture()
    with caplog.at_level(logging.INFO, logger="summon.api.app"), TestClient(app) as client:
        attach_session(client, secret)
        response = client.put(
            "/api/groups/-100/notes/private-name",
            json={"content": "UNIQUE_NOTE_BODY", "expected_revision": None},
            headers={"X-Summon-Request": "1"},
        )
        assert response.status_code == 200
    assert "/api/groups/{group_id}/notes/{name}" in caplog.text
    assert "private-name" not in caplog.text and "UNIQUE_NOTE_BODY" not in caplog.text


def test_corrupt_note_storage_error_is_sanitized_for_api_clients():
    app, store, _, secret = api_fixture()
    store.notes.list.side_effect = NoteStorageError("sensitive broken contents")
    with TestClient(app) as client:
        attach_session(client, secret)
        response = client.get("/api/groups/-100/notes")
    assert response.status_code == 503
    assert "operator review" in response.text
    assert "sensitive broken contents" not in response.text


async def test_note_length_counts_unicode_codepoints_and_rejects_longer_text():
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=member()))
    notes = SimpleNamespace(save=AsyncMock(return_value={"name": "emoji"}))
    service = GroupNotesService(bot, notes)
    await service.save(-100, 42, "emoji", "😀" * 1200, None, NOW)
    assert len(notes.save.await_args.args[3]) == 1200
    with pytest.raises(ValueError):
        await service.save(-100, 42, "emoji", "😀" * 1201, None, NOW)
    assert notes.save.await_count == 1
