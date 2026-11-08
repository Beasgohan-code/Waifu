"""Isolated browser-auth fixture: fake users/store, no MongoDB or Telegram network.

Run only for the documented local smoke test with SUMMON_SESSION_TEST_FIXTURE=1.
Never use these fixed test credentials or in-memory repositories in deployment.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta
from typing import Any

from summon.api.app import create_app
from summon.api.preview import PreviewStore, PreviewUsers
from summon.auth import TelegramUser, launch_fingerprint
from summon.config import Settings


class FixtureUsers(PreviewUsers):
    def __init__(self) -> None:
        self.names: dict[int, str] = {}

    async def touch(self, user: TelegramUser, now: datetime) -> None:
        self.names[user.id] = user.first_name

    async def profile(self, user_id: int) -> dict[str, Any]:
        profile = await super().profile(user_id)
        profile.update(id=user_id, first_name=self.names[user_id], username=None)
        return profile


class FixtureAuth:
    def __init__(self) -> None:
        self.proofs: set[str] = set()

    async def consume(self, proof: str, now: datetime, max_age: int) -> bool:
        fingerprint = launch_fingerprint(proof)
        if fingerprint in self.proofs:
            return False
        self.proofs.add(fingerprint)
        return True


class FixtureSessions:
    def __init__(self) -> None:
        self.records: dict[str, tuple[int, datetime]] = {}

    async def create(self, session_id: str, user_id: int, now: datetime, max_age: int) -> None:
        key = hashlib.sha256(session_id.encode()).hexdigest()
        self.records[key] = (user_id, now + timedelta(seconds=max_age))

    async def is_active(self, session_id: str, user_id: int, now: datetime) -> bool:
        key = hashlib.sha256(session_id.encode()).hexdigest()
        record = self.records.get(key)
        return bool(record and record[0] == user_id and record[1] > now)

    async def revoke(self, session_id: str, user_id: int) -> None:
        key = hashlib.sha256(session_id.encode()).hexdigest()
        if key in self.records and self.records[key][0] == user_id:
            del self.records[key]


class FixtureLimiter:
    async def allow(self, key: str, *, limit: int, seconds: int) -> bool:
        return True


def make_fixture_app():
    store = PreviewStore()
    store.users = FixtureUsers()
    store.auth = FixtureAuth()
    store.limiter = FixtureLimiter()
    store.sessions = FixtureSessions()
    embedded = os.environ.get("SUMMON_SESSION_TEST_EMBEDDED") == "1"
    web_origin = "https://app.summon.test:19443"
    settings = Settings(
        environment="production" if embedded else "development",
        preview_mode=False,
        rich_enabled=False,
        bot_token="123456:TEST_TOKEN_DO_NOT_USE",
        session_secret="test-session-secret-not-for-production",
        mongo_uri="mongodb://example.invalid" if embedded else "",
        webapp_url=web_origin if embedded else "",
        allowed_origins=[web_origin] if embedded else [],
        telegram_api_id=12345 if embedded else None,
        telegram_api_hash="synthetic-hash" if embedded else "",
        _env_file=None,
    )
    return create_app(settings=settings, store=store)


if __name__ == "__main__":
    if os.environ.get("SUMMON_SESSION_TEST_FIXTURE") != "1":
        raise SystemExit("Set SUMMON_SESSION_TEST_FIXTURE=1 for this isolated test fixture")
    import uvicorn

    # Only run in an isolated test environment; the preview proxy needs this bind.
    uvicorn.run(make_fixture_app(), host="0.0.0.0", port=8000, log_level="warning")  # noqa: S104
