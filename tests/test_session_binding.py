"""Cookie identity must not bleed between Telegram launches sharing browser storage."""

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode

from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from summon.api.app import create_app
from summon.config import Settings

TOKEN = "123456:TEST_TOKEN_DO_NOT_USE"
SECRET = "s" * 40


def telegram_launch(user_id: int) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "first_name": f"User {user_id}"}),
    }
    data = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def binding(init_data: str) -> str:
    proof = parse_qs(init_data)["hash"][0]
    return hashlib.sha256(proof.encode("ascii")).hexdigest()


def make_client(*, production: bool = False) -> TestClient:
    consumed: set[str] = set()

    async def consume(proof: str, *_args) -> bool:
        if proof in consumed:
            return False
        consumed.add(proof)
        return True

    sessions: dict[str, tuple[int, datetime]] = {}

    async def create_session(session_id, user_id, now, max_age):
        key = hashlib.sha256(session_id.encode()).hexdigest()
        sessions[key] = (user_id, now + timedelta(seconds=max_age))

    async def is_active(session_id, user_id, now):
        key = hashlib.sha256(session_id.encode()).hexdigest()
        record = sessions.get(key)
        return bool(record and record[0] == user_id and record[1] > now)

    async def revoke_session(session_id, user_id):
        key = hashlib.sha256(session_id.encode()).hexdigest()
        if key in sessions and sessions[key][0] == user_id:
            del sessions[key]

    users = SimpleNamespace(
        touch=AsyncMock(),
        profile=AsyncMock(side_effect=lambda user_id: {"id": user_id, "first_name": "User"}),
    )
    store = SimpleNamespace(
        users=users,
        sessions=SimpleNamespace(
            create=AsyncMock(side_effect=create_session),
            is_active=AsyncMock(side_effect=is_active),
            revoke=AsyncMock(side_effect=revoke_session),
        ),
        auth=SimpleNamespace(consume=AsyncMock(side_effect=consume)),
        limiter=SimpleNamespace(allow=AsyncMock(return_value=True)),
    )
    production_values = (
        {
            "environment": "production",
            "mongo_uri": "mongodb://example.invalid",
            "webapp_url": "https://miniapp.example",
            "telegram_api_id": 12345,
            "telegram_api_hash": "synthetic-hash",
        }
        if production
        else {}
    )
    settings = Settings(bot_token=TOKEN, session_secret=SECRET, _env_file=None, **production_values)
    base_url = "https://miniapp.example" if production else "http://testserver"
    return TestClient(create_app(settings=settings, store=store), base_url=base_url)


def sign_in(client: TestClient, launch: str):
    return client.post(
        "/api/auth/session",
        json={"init_data": launch},
        headers={"X-Summon-Request": "1", "X-Summon-Launch": binding(launch)},
    )


def test_cookie_requires_its_own_launch_and_never_serves_a_different_account():
    launch_a, launch_b = telegram_launch(123), telegram_launch(456)
    with make_client() as client:
        assert sign_in(client, launch_a).status_code == 200
        assert client.get("/api/me").status_code == 401
        assert (
            client.get("/api/me", headers={"X-Summon-Launch": binding(launch_b)}).status_code == 401
        )
        assert (
            client.get("/api/me", headers={"X-Summon-Launch": binding(launch_a)}).json()["data"][
                "id"
            ]
            == 123
        )

        # A second Telegram account may share the cookie jar. It must get its own
        # verified identity, and the first tab must not see the second account.
        assert sign_in(client, launch_b).status_code == 200
        assert (
            client.get("/api/me", headers={"X-Summon-Launch": binding(launch_b)}).json()["data"][
                "id"
            ]
            == 456
        )
        assert (
            client.get("/api/me", headers={"X-Summon-Launch": binding(launch_a)}).status_code == 401
        )
        assert (
            client.post(
                "/api/auth/logout",
                headers={"X-Summon-Request": "1", "X-Summon-Launch": binding(launch_a)},
            ).status_code
            == 401
        )
        assert (
            client.get("/api/me", headers={"X-Summon-Launch": binding(launch_b)}).status_code == 200
        )


def test_consumed_proof_cannot_reissue_a_cookie_after_account_switch():
    launch_a, launch_b = telegram_launch(123), telegram_launch(456)
    with make_client() as client:
        assert sign_in(client, launch_a).status_code == 200
        assert sign_in(client, launch_b).status_code == 200
        assert sign_in(client, launch_a).status_code == 401
        assert (
            client.get("/api/me", headers={"X-Summon-Launch": binding(launch_a)}).status_code == 401
        )
        assert (
            client.get("/api/me", headers={"X-Summon-Launch": binding(launch_b)}).json()["data"][
                "id"
            ]
            == 456
        )


def test_legacy_unbound_cookie_is_rejected():
    launch = telegram_launch(123)
    # Old cookies signed only a user object, without a launch binding.
    old_cookie = URLSafeTimedSerializer(SECRET, salt="summon.player-session.v1").dumps(
        {"id": 123, "first_name": "User"}
    )
    with make_client() as client:
        client.cookies.set("summon_session", old_cookie, path="/api")
        assert (
            client.get("/api/me", headers={"X-Summon-Launch": binding(launch)}).status_code == 401
        )
        bound_but_stateless_cookie = URLSafeTimedSerializer(
            SECRET, salt="summon.player-session.v1"
        ).dumps({"user": {"id": 123, "first_name": "User"}, "launch_digest": binding(launch)})
        client.cookies.set("summon_session", bound_but_stateless_cookie, path="/api")
        assert (
            client.get("/api/me", headers={"X-Summon-Launch": binding(launch)}).status_code == 401
        )


def test_login_header_mismatch_does_not_consume_the_signed_proof():
    launch = telegram_launch(123)
    with make_client() as client:
        wrong = client.post(
            "/api/auth/session",
            json={"init_data": launch},
            headers={"X-Summon-Request": "1", "X-Summon-Launch": "0" * 64},
        )
        assert wrong.status_code == 401
        # A corrected request with the same proof must still succeed.
        assert sign_in(client, launch).status_code == 200
        assert (
            client.get("/api/inventory", headers={"X-Summon-Launch": "0" * 64}).status_code == 401
        )


def test_cookie_contains_no_raw_telegram_signature_and_valid_logout_clears_it():
    launch = telegram_launch(123)
    with make_client() as client:
        assert sign_in(client, launch).status_code == 200
        cookie = client.cookies.get("summon_session")
        payload = URLSafeTimedSerializer(SECRET, salt="summon.player-session.v1").loads(cookie)
        assert payload["launch_digest"] == binding(launch)
        assert parse_qs(launch)["hash"][0] not in json.dumps(payload)
        headers = {"X-Summon-Request": "1", "X-Summon-Launch": binding(launch)}
        assert client.post("/api/auth/logout", headers=headers).status_code == 200
        assert client.get("/api/me", headers=headers).status_code == 401
        # Replaying a copied cookie after logout must not restore the session.
        client.cookies.set("summon_session", cookie, domain="testserver.local", path="/api")
        assert client.get("/api/me", headers=headers).status_code == 401


def test_launch_header_alone_cannot_authenticate():
    launch = telegram_launch(123)
    with make_client() as client:
        assert (
            client.get("/api/me", headers={"X-Summon-Launch": binding(launch)}).status_code == 401
        )
        # Knowing a fingerprint does not replace Telegram's signed launch data.
        response = client.post(
            "/api/auth/session",
            json={"init_data": "auth_date=1&user=invalid&hash=" + "a" * 64},
            headers={"X-Summon-Request": "1", "X-Summon-Launch": binding(launch)},
        )
        assert response.status_code == 401
        assert (
            client.get("/api/me", headers={"X-Summon-Launch": binding(launch)}).status_code == 401
        )


def test_explicit_cors_allows_binding_header_only_for_configured_origins():
    settings = Settings(
        bot_token=TOKEN,
        session_secret=SECRET,
        allowed_origins=["https://miniapp.example"],
        _env_file=None,
    )
    with TestClient(create_app(settings=settings, store=object())) as client:
        allowed = client.options(
            "/api/me",
            headers={
                "Origin": "https://miniapp.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-summon-launch",
            },
        )
        assert allowed.status_code == 200
        assert "x-summon-launch" in allowed.headers["access-control-allow-headers"].lower()
        forbidden = client.options(
            "/api/me",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-summon-launch",
            },
        )
        assert forbidden.status_code == 400
        assert "access-control-allow-origin" not in forbidden.headers


def test_logging_out_one_session_does_not_revoke_another():
    launch_a, launch_b = telegram_launch(123), telegram_launch(456)
    with make_client() as client:
        assert sign_in(client, launch_a).status_code == 200
        cookie_a = client.cookies.get("summon_session")
        assert sign_in(client, launch_b).status_code == 200
        cookie_b = client.cookies.get("summon_session")
        assert (
            client.post(
                "/api/auth/logout",
                headers={"X-Summon-Request": "1", "X-Summon-Launch": binding(launch_b)},
            ).status_code
            == 200
        )
        client.cookies.set("summon_session", cookie_a, domain="testserver.local", path="/api")
        assert (
            client.get("/api/me", headers={"X-Summon-Launch": binding(launch_a)}).status_code == 200
        )
        client.cookies.set("summon_session", cookie_b, domain="testserver.local", path="/api")
        assert (
            client.get("/api/me", headers={"X-Summon-Launch": binding(launch_b)}).status_code == 401
        )


def test_session_store_failure_never_authenticates_or_reports_logout_success():
    from pymongo.errors import OperationFailure

    launch = telegram_launch(123)
    headers = {"X-Summon-Request": "1", "X-Summon-Launch": binding(launch)}
    with make_client() as client:
        assert sign_in(client, launch).status_code == 200
        sessions = client.app.state.store.sessions
        sessions.is_active.side_effect = OperationFailure("internal detail", code=13)
        failure = client.get("/api/me", headers=headers)
        assert failure.status_code == 503
        assert failure.json()["error"]["code"] == "database_unavailable"
        assert "internal detail" not in failure.text
        sessions.is_active.side_effect = None
        sessions.is_active.return_value = True
        sessions.revoke.side_effect = OperationFailure("internal detail", code=13)
        failure = client.post("/api/auth/logout", headers=headers)
        assert failure.status_code == 503
        assert "set-cookie" not in failure.headers
        assert "internal detail" not in failure.text


def test_session_creation_failure_does_not_issue_cookie_or_allow_replay():
    from pymongo.errors import OperationFailure

    launch = telegram_launch(123)
    with make_client() as client:
        client.app.state.store.sessions.create.side_effect = OperationFailure(
            "internal detail", code=13
        )
        failure = sign_in(client, launch)
        assert failure.status_code == 503
        assert "set-cookie" not in failure.headers
        assert sign_in(client, launch).status_code == 401


def test_embedded_cookie_migration_is_bound_and_never_downgrades_to_legacy():
    launch = telegram_launch(123)
    headers = {"X-Summon-Launch": binding(launch)}
    with make_client(production=True) as client:
        assert sign_in(client, launch).status_code == 200
        current = client.cookies.get("summon_session_v2")
        assert current
        client.cookies.clear()
        # Existing signed and active legacy sessions remain usable until expiry.
        client.cookies.set("summon_session", current, domain="miniapp.example", path="/api")
        assert client.get("/api/me", headers=headers).json()["data"]["id"] == 123
        # A present but invalid v2 cookie must not silently use the legacy one.
        client.cookies.set("summon_session_v2", "invalid", domain="miniapp.example", path="/api")
        assert client.get("/api/me", headers=headers).status_code == 401
        client.cookies.set("summon_session_v2", current, domain="miniapp.example", path="/api")
        assert client.get("/api/me", headers=headers).status_code == 200

        # Cookie policy does not relax the CSRF origin and request-header checks.
        logout_headers = {**headers, "X-Summon-Request": "1"}
        denied = client.post(
            "/api/auth/logout", headers={**logout_headers, "Origin": "https://attacker.example"}
        )
        assert denied.status_code == 403
        assert client.post("/api/auth/logout", headers=headers).status_code == 403
        assert client.post("/api/auth/logout", headers=logout_headers).status_code == 200
        # A copied embedded cookie remains revoked after logout.
        client.cookies.set("summon_session_v2", current, domain="miniapp.example", path="/api")
        assert client.get("/api/me", headers=headers).status_code == 401
