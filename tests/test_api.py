import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode

import pytest
from fastapi.testclient import TestClient

from summon.api.app import create_app
from summon.config import Settings

TOKEN = "123456:TEST_TOKEN_DO_NOT_USE"
SECRET = "s" * 40


def init_data(user_id=123):
    fields = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "first_name": "Mika"}),
    }
    data = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def sign_in(client, user_id=123):
    launch = init_data(user_id)
    proof = parse_qs(launch)["hash"][0]
    client.headers["X-Summon-Launch"] = hashlib.sha256(proof.encode()).hexdigest()
    return client.post(
        "/api/auth/session", json={"init_data": launch}, headers={"X-Summon-Request": "1"}
    )


def fake_sessions():
    return SimpleNamespace(
        create=AsyncMock(), is_active=AsyncMock(return_value=True), revoke=AsyncMock()
    )


@pytest.fixture
def client():
    users = SimpleNamespace(
        touch=AsyncMock(),
        profile=AsyncMock(side_effect=lambda user_id: {"id": user_id, "first_name": "Mika"}),
        collection_insights=AsyncMock(side_effect=lambda user_id: {"owner": user_id}),
        leaderboard=AsyncMock(return_value=[]),
    )
    store = SimpleNamespace(
        users=users,
        sessions=fake_sessions(),
        auth=SimpleNamespace(consume=AsyncMock(return_value=True)),
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
    )
    settings = Settings(bot_token=TOKEN, session_secret=SECRET, _env_file=None)
    with TestClient(create_app(settings=settings, store=store)) as test_client:
        yield test_client


def test_authenticated_session_uses_signed_telegram_identity(client):
    response = sign_in(client)
    assert response.status_code == 200
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    assert client.get("/api/me?user_id=999").json()["data"]["id"] == 123


def test_invalid_or_missing_identity_cannot_access_private_api(client):
    assert client.get("/api/me").status_code == 401
    response = client.post(
        "/api/auth/session", json={"init_data": "bad"}, headers={"X-Summon-Request": "1"}
    )
    assert response.status_code == 401


def test_mutating_requests_need_custom_header_and_same_origin(client):
    assert client.post("/api/auth/session", json={"init_data": init_data()}).status_code == 403
    result = client.post(
        "/api/auth/session",
        json={"init_data": init_data()},
        headers={"X-Summon-Request": "1", "Origin": "https://evil.example"},
    )
    assert result.status_code == 403


def test_preview_cannot_be_enabled_in_production():
    with pytest.raises(ValueError):
        Settings(environment="production", preview_mode=True, _env_file=None)


def test_catalog_admin_endpoint_rejects_non_operator(client):
    assert sign_in(client).status_code == 200
    result = client.put(
        "/api/admin/characters/aiko",
        headers={"X-Summon-Request": "1"},
        json={
            "id": "aiko",
            "name": "Aiko",
            "category": "Sky",
            "rarity": "rare",
            "image_url": "/art/aiko.png",
        },
    )
    assert result.status_code == 403


def test_group_settings_recheck_telegram_role_for_authenticated_user():
    store = SimpleNamespace(
        users=SimpleNamespace(touch=AsyncMock(), profile=AsyncMock()),
        sessions=fake_sessions(),
        auth=SimpleNamespace(consume=AsyncMock(return_value=True)),
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
        groups=SimpleNamespace(update_settings=AsyncMock()),
    )
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")))
    settings = Settings(bot_token=TOKEN, session_secret=SECRET, _env_file=None)
    with TestClient(create_app(settings=settings, store=store, telegram_bot=bot)) as client:
        assert sign_in(client).status_code == 200
        result = client.patch(
            "/api/groups/-100/settings",
            json={"spawn_enabled": True, "actor_id": 999},
            headers={"X-Summon-Request": "1"},
        )
        assert result.status_code == 422  # An extra actor_id is never accepted.
        result = client.patch(
            "/api/groups/-100/settings",
            json={"spawn_enabled": True},
            headers={"X-Summon-Request": "1"},
        )
        assert result.status_code == 403
        bot.get_chat_member.assert_awaited_with(-100, 123)
        store.groups.update_settings.assert_not_awaited()


def test_read_only_preview_exposes_only_sample_reads():
    settings = Settings(preview_mode=True, _env_file=None)
    with TestClient(create_app(settings=settings)) as preview:
        assert preview.get("/api/bootstrap").json()["data"]["preview"] is True
        assert preview.get("/api/characters").json()["data"]["total"] == 3
        assert preview.get("/api/inventory?favorite=true").json()["data"]["total"] == 1
        assert preview.get("/api/groups/-100").json()["data"]["title"] == "The Collector's Club"
        assert preview.post("/api/daily", headers={"X-Summon-Request": "1"}).status_code == 403


def test_gift_api_uses_only_signed_identity_and_validates_input():
    gifts = SimpleNamespace(
        create=AsyncMock(return_value={"id": "a" * 32}),
        confirm=AsyncMock(return_value={"status": "completed"}),
        list=AsyncMock(return_value=[]),
        cancel=AsyncMock(),
    )
    store = SimpleNamespace(
        users=SimpleNamespace(touch=AsyncMock(), profile=AsyncMock()),
        sessions=fake_sessions(),
        auth=SimpleNamespace(consume=AsyncMock(return_value=True)),
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
        gifts=gifts,
    )
    settings = Settings(bot_token=TOKEN, session_secret=SECRET, _env_file=None)
    with TestClient(create_app(settings=settings, store=store)) as api_client:
        headers = {"X-Summon-Request": "1"}
        assert sign_in(api_client).status_code == 200
        payload = {"receiver_id": 456, "character_id": "aiko"}
        assert (
            api_client.post(
                "/api/gifts", json={**payload, "sender_id": 999}, headers=headers
            ).status_code
            == 422
        )
        assert api_client.post("/api/gifts", json=payload, headers=headers).status_code == 201
        assert gifts.create.await_args.args[:3] == (123, 456, "aiko")
        assert api_client.get("/api/gifts?user_id=999").status_code == 200
        gifts.list.assert_awaited_once_with(123)
        assert api_client.post(f"/api/gifts/{'a' * 32}/accept", headers=headers).status_code == 200
        assert gifts.confirm.await_args.args[:2] == ("a" * 32, 123)


def test_catalog_operator_audit_uses_verified_session_actor():
    catalog = SimpleNamespace(upsert_character=AsyncMock(return_value={"id": "aiko"}))
    store = SimpleNamespace(
        catalog=catalog,
        users=SimpleNamespace(touch=AsyncMock()),
        sessions=fake_sessions(),
        auth=SimpleNamespace(consume=AsyncMock(return_value=True)),
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
    )
    settings = Settings(bot_token=TOKEN, session_secret=SECRET, operator_ids=[123], _env_file=None)
    with TestClient(create_app(settings=settings, store=store)) as client:
        assert sign_in(client).status_code == 200
        response = client.put(
            "/api/admin/characters/aiko",
            headers={"X-Summon-Request": "1"},
            json={
                "id": "aiko",
                "name": "Aiko",
                "category": "Sky",
                "rarity": "rare",
                "image_url": "/art/aiko.webp",
            },
        )
        assert response.status_code == 200
    assert catalog.upsert_character.await_args.kwargs["actor_id"] == 123


def test_goodbye_settings_require_live_change_info_right_and_session_actor():
    groups = SimpleNamespace(update_settings=AsyncMock(return_value={"id": -100}))
    store = SimpleNamespace(
        users=SimpleNamespace(touch=AsyncMock()),
        sessions=fake_sessions(),
        auth=SimpleNamespace(consume=AsyncMock(return_value=True)),
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
        groups=groups,
    )
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(
                status="administrator", privileges=SimpleNamespace(can_change_info=False)
            )
        )
    )
    settings = Settings(bot_token=TOKEN, session_secret=SECRET, _env_file=None)
    with TestClient(create_app(settings=settings, store=store, telegram_bot=bot)) as api_client:
        headers = {"X-Summon-Request": "1"}
        assert sign_in(api_client).status_code == 200
        payload = {"goodbye_enabled": True, "goodbye_text": "Take care!"}
        spoofed = api_client.patch(
            "/api/groups/-100/settings", json={**payload, "actor_id": 999}, headers=headers
        )
        assert spoofed.status_code == 422
        denied = api_client.patch("/api/groups/-100/settings", json=payload, headers=headers)
        assert denied.status_code == 403
        groups.update_settings.assert_not_awaited()
        bot.get_chat_member.return_value = SimpleNamespace(
            status="administrator", privileges=SimpleNamespace(can_change_info=True)
        )
        result = api_client.patch("/api/groups/-100/settings", json=payload, headers=headers)
        assert result.status_code == 200
        assert groups.update_settings.await_args.args[:3] == (-100, 123, payload)
        invalid = api_client.patch(
            "/api/groups/-100/settings", json={"goodbye_text": "x" * 1001}, headers=headers
        )
        assert invalid.status_code == 422
        assert groups.update_settings.await_count == 1


def test_production_session_cookie_is_secure_and_requires_https():
    store = SimpleNamespace(
        users=SimpleNamespace(
            touch=AsyncMock(),
            profile=AsyncMock(side_effect=lambda user_id: {"id": user_id, "first_name": "Mika"}),
        ),
        sessions=fake_sessions(),
        auth=SimpleNamespace(consume=AsyncMock(return_value=True)),
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
    )
    settings = Settings(
        environment="production",
        bot_token=TOKEN,
        session_secret=SECRET,
        mongo_uri="mongodb://example.invalid",
        webapp_url="https://miniapp.example",
        telegram_api_id=12345,
        telegram_api_hash="test-hash",
        _env_file=None,
    )
    with TestClient(
        create_app(settings=settings, store=store), base_url="https://miniapp.example"
    ) as client:
        response = sign_in(client)
        assert response.status_code == 200
        cookies = response.headers.get_list("set-cookie")
        assert len(cookies) == 2
        cookie = next(value.lower() for value in cookies if value.startswith("summon_session_v2="))
        assert "secure" in cookie and "httponly" in cookie and "samesite=none" in cookie
        assert "partitioned" in cookie and "path=/api" in cookie
        assert any(
            value.startswith("summon_session=") and "Max-Age=0" in value for value in cookies
        )
        assert client.get("/api/me").status_code == 200
        # This only checks client cookie behavior; real TLS/proxy setup is a release gate.
        assert client.get("http://miniapp.example/api/me").status_code == 401
        logout = client.post("/api/auth/logout", headers={"X-Summon-Request": "1"})
        cleared = logout.headers.get_list("set-cookie")
        embedded = next(
            value.lower() for value in cleared if value.startswith("summon_session_v2=")
        )
        assert "partitioned" in embedded and "max-age=0" in embedded and "secure" in embedded
        assert any(
            value.startswith("summon_session=") and "Max-Age=0" in value for value in cleared
        )


@pytest.mark.parametrize("premium", [True, False])
def test_bootstrap_exposes_only_presentation_metadata_for_native_collection_button(premium):
    from summon.bot.presentation import Emoji

    settings = Settings(preview_mode=True, premium_emoji_enabled=premium, _env_file=None)
    with TestClient(create_app(settings=settings)) as client:
        data = client.get("/api/bootstrap").json()["data"]
    assert data == {
        "preview": True,
        "collection_button_emoji": {
            "fallback": Emoji.OPEN_COLLECTION.value[0],
            "custom_emoji_id": Emoji.OPEN_COLLECTION.value[1] if premium else None,
        },
    }


def test_collection_insights_require_session_and_ignore_user_supplied_identity(client):
    assert client.get("/api/me/collection-insights?user_id=999").status_code == 401
    assert sign_in(client, 123).status_code == 200
    result = client.get("/api/me/collection-insights?user_id=999")
    assert result.status_code == 200
    assert result.json()["data"]["owner"] == 123
    client.app.state.store.users.collection_insights.assert_awaited_once_with(123)


def test_collection_insights_fail_closed_when_limited_or_database_is_unavailable(client):
    from pymongo.errors import AutoReconnect

    assert sign_in(client).status_code == 200
    store = client.app.state.store
    store.limiter.allow.return_value = False
    assert client.get("/api/me/collection-insights").status_code == 429
    store.users.collection_insights.assert_not_awaited()
    store.limiter.allow.return_value = True
    store.users.collection_insights.side_effect = AutoReconnect("FAKE_PRIVATE_DB_DETAIL")
    result = client.get("/api/me/collection-insights")
    assert result.status_code == 503
    assert "FAKE_PRIVATE_DB_DETAIL" not in result.text


def test_reviewed_catalog_endpoints_require_operator_and_pass_revision_and_verified_actor():
    from summon.repositories.catalog import CatalogStaleRevision

    character = {
        "id": "aiko",
        "name": "Aiko",
        "category": "Sky",
        "rarity": "rare",
        "image_url": "/art/aiko.webp",
    }
    revision = "a" * 64
    catalog = SimpleNamespace(
        edit_state=AsyncMock(return_value={"revision": revision, "character": character}),
        upsert_character=AsyncMock(return_value=character),
    )
    store = SimpleNamespace(
        catalog=catalog,
        users=SimpleNamespace(touch=AsyncMock()),
        sessions=fake_sessions(),
        auth=SimpleNamespace(consume=AsyncMock(return_value=True)),
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
    )
    settings = Settings(bot_token=TOKEN, session_secret=SECRET, operator_ids=[123], _env_file=None)
    with TestClient(create_app(settings=settings, store=store)) as client:
        path = "/api/admin/characters/aiko"
        assert client.get(f"{path}/edit-state").status_code == 401
        assert sign_in(client, 124).status_code == 200
        assert client.get(f"{path}/edit-state").status_code == 403
        catalog.edit_state.assert_not_awaited()
        assert sign_in(client, 123).status_code == 200
        response = client.get(f"{path}/edit-state")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["data"]["revision"] == revision
        catalog.edit_state.assert_awaited_once_with("aiko", secret=SECRET)
        payload = {"character": character, "expected_revision": revision}
        assert client.put(f"{path}/reviewed", json=payload).status_code == 403
        assert (
            client.put(
                f"{path}/reviewed",
                json={**payload, "expected_revision": "bad"},
                headers={"X-Summon-Request": "1"},
            ).status_code
            == 422
        )
        response = client.put(f"{path}/reviewed", json=payload, headers={"X-Summon-Request": "1"})
        assert response.status_code == 200
        assert catalog.upsert_character.await_args.kwargs == {
            "actor_id": 123,
            "expected_revision": revision,
            "revision_secret": SECRET,
        }
        catalog.upsert_character.side_effect = CatalogStaleRevision()
        stale = client.put(f"{path}/reviewed", json=payload, headers={"X-Summon-Request": "1"})
        assert stale.status_code == 409
        assert "review" in stale.json()["error"]["message"].lower()
        mismatch = client.put(
            f"{path}/reviewed",
            json={**payload, "character": {**character, "id": "other"}},
            headers={"X-Summon-Request": "1"},
        )
        assert mismatch.status_code == 400


def test_preview_operator_can_review_fictional_catalog_but_cannot_save():
    settings = Settings(preview_mode=True, operator_ids=[1], _env_file=None)
    with TestClient(create_app(settings=settings)) as client:
        state = client.get("/api/admin/characters/aiko/edit-state")
        assert state.status_code == 200
        data = state.json()["data"]
        assert data["character"]["name"] == "Aiko Tsukino"
        assert "telegram_file_id" not in data["character"]
        assert len(data["revision"]) == 64
        absent = client.get("/api/admin/characters/fictional/edit-state")
        assert absent.status_code == 200 and absent.json()["data"]["character"] is None
        response = client.put(
            "/api/admin/characters/aiko/reviewed",
            json={
                "character": {
                    "id": "aiko",
                    "name": "Sample",
                    "category": "Sample",
                    "rarity": "rare",
                    "image_url": "/art/aiko.webp",
                },
                "expected_revision": data["revision"],
            },
            headers={"X-Summon-Request": "1"},
        )
        assert response.status_code == 403


def test_group_summon_insights_require_current_admin_and_bind_actor():
    from summon.repositories.group_insights import GroupInsightsDataError

    report = {
        "window_days": 30,
        "attempts": 4,
        "caught": 2,
        "expired": 1,
        "delivery_rejected": 0,
        "awaiting_resolution": 1,
        "my_catches": 1,
    }
    collection = SimpleNamespace(group_summon_insights=AsyncMock(return_value=report))
    groups = SimpleNamespace(get=AsyncMock(return_value={"_id": -100}))
    store = SimpleNamespace(
        collection=collection,
        groups=groups,
        users=SimpleNamespace(touch=AsyncMock()),
        sessions=fake_sessions(),
        auth=SimpleNamespace(consume=AsyncMock(return_value=True)),
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
    )
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")))
    settings = Settings(bot_token=TOKEN, session_secret=SECRET, _env_file=None)
    with TestClient(create_app(settings=settings, store=store, telegram_bot=bot)) as client:
        path = "/api/groups/-100/summon-insights"
        assert client.get(path).status_code == 401
        assert sign_in(client).status_code == 200
        assert client.get(path).status_code == 403
        collection.group_summon_insights.assert_not_awaited()
        bot.get_chat_member.return_value = SimpleNamespace(status="administrator")
        response = client.get(path + "?user_id=999")
        assert response.status_code == 200
        assert response.json()["data"]["my_catches"] == 1
        assert collection.group_summon_insights.await_args.args[:2] == (-100, 123)
        assert client.get("/api/groups/1/summon-insights").status_code == 422
        collection.group_summon_insights.side_effect = GroupInsightsDataError()
        assert client.get(path).status_code == 503
        groups.get.return_value = None
        assert client.get(path).status_code == 404


def test_preview_group_summon_insights_are_fictional_and_group_scoped():
    with TestClient(create_app(settings=Settings(preview_mode=True, _env_file=None))) as client:
        response = client.get("/api/groups/-100/summon-insights")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["window_days"] == 30 and data["attempts"] == 8
        assert "claimed_by" not in data and "user_id" not in data
        assert client.get("/api/groups/-999/summon-insights").status_code == 404
