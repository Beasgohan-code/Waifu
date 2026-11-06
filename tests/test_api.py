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
            return_value=SimpleNamespace(status="administrator", can_change_info=False)
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
            status="administrator", can_change_info=True
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
        _env_file=None,
    )
    with TestClient(
        create_app(settings=settings, store=store), base_url="https://miniapp.example"
    ) as client:
        response = sign_in(client)
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        assert client.get("/api/me").status_code == 200
        # This only checks client cookie behavior; real TLS/proxy setup is a release gate.
        assert client.get("http://miniapp.example/api/me").status_code == 401
