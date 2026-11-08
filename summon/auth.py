"""Telegram Mini App authentication and short-lived, server-signed sessions."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

TELEGRAM_FUTURE_TOLERANCE_SECONDS = 30


class AuthenticationError(ValueError):
    """Authentication failed; never disclose signature details to clients."""


class TelegramUser(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int = Field(gt=0, le=2**53 - 1, strict=True)
    first_name: str = Field(min_length=1, max_length=128)
    last_name: str | None = Field(default=None, max_length=128)
    username: str | None = Field(default=None, max_length=64)
    photo_url: str | None = Field(default=None, max_length=2048)


def verify_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age: int = 3600,
    now: int | None = None,
    future_tolerance: int = TELEGRAM_FUTURE_TOLERANCE_SECONDS,
) -> TelegramUser:
    """Verify Telegram's HMAC, field uniqueness, bounded age and signed user.

    See https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app.
    Only this result, never ``initDataUnsafe`` or a client ID, is an identity.
    """
    if not bot_token or not init_data or len(init_data) > 8192 or max_age < 1:
        raise AuthenticationError("Invalid Telegram credentials")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError("duplicate field")
        provided_hash = fields.pop("hash")
        if len(provided_hash) != 64 or any(c not in "0123456789abcdef" for c in provided_hash):
            raise ValueError("invalid hash")
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_hash, provided_hash):
            raise ValueError("invalid signature")
        auth_date = int(fields["auth_date"])
        current = int(time.time()) if now is None else now
        if auth_date > current + future_tolerance or current - auth_date > max_age:
            raise ValueError("expired authentication")
        return TelegramUser.model_validate(json.loads(fields["user"]))
    except (KeyError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        raise AuthenticationError("Invalid Telegram credentials") from exc


def launch_fingerprint(proof: str) -> str:
    """Hash the verified Telegram signature; never store the raw launch proof."""
    return hashlib.sha256(proof.encode("utf-8")).hexdigest()


class BoundSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: TelegramUser
    launch_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


class SessionCodec:
    """Sign identity and launch binding; authorization is rechecked on the server."""

    def __init__(self, secret: str, *, max_age: int = 3600) -> None:
        if len(secret) < 32:
            raise ValueError("Session secret must contain at least 32 characters")
        self._serializer = URLSafeTimedSerializer(secret, salt="summon.player-session.v1")
        self.max_age = max_age

    def issue(self, user: TelegramUser, *, launch_digest: str, session_id: str) -> str:
        session = BoundSession(user=user, launch_digest=launch_digest, session_id=session_id)
        return self._serializer.dumps(session.model_dump(exclude_none=True))

    def verify(self, token: str, *, launch_digest: str) -> BoundSession:
        if (
            not token
            or len(token) > 4096
            or len(launch_digest) != 64
            or any(c not in "0123456789abcdef" for c in launch_digest)
        ):
            raise AuthenticationError("Invalid session")
        try:
            session = BoundSession.model_validate(
                self._serializer.loads(token, max_age=self.max_age)
            )
            if not hmac.compare_digest(session.launch_digest, launch_digest):
                raise AuthenticationError("Invalid session")
            return session
        except (BadSignature, SignatureExpired, ValidationError, TypeError, ValueError) as exc:
            raise AuthenticationError("Invalid session") from exc
