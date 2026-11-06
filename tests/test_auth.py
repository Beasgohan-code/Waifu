import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from summon.auth import AuthenticationError, SessionCodec, verify_init_data

TOKEN = "123456:TEST_TOKEN_DO_NOT_USE"
NOW = 1_780_000_000


def signed_init_data(*, token=TOKEN, timestamp=NOW, user=None, extra=None):
    fields = {
        "auth_date": str(timestamp),
        "query_id": "query-1",
        "user": json.dumps(user or {"id": 123, "first_name": "Mika"}, separators=(",", ":")),
        **(extra or {}),
    }
    text = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, text.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_valid_telegram_signature_and_identity():
    user = verify_init_data(signed_init_data(), TOKEN, now=NOW, max_age=300)
    assert user.id == 123
    assert user.first_name == "Mika"


@pytest.mark.parametrize(
    "raw, now",
    [
        (signed_init_data(timestamp=NOW - 301), NOW),
        (signed_init_data(timestamp=NOW + 31), NOW),
        (signed_init_data(token="other-token"), NOW),
        (signed_init_data() + "&auth_date=1", NOW),
        (signed_init_data() + "&hash=bad", NOW),
        (signed_init_data(user={"id": -4, "first_name": "No"}), NOW),
        ("not=telegram", NOW),
        ("x=" + "a" * 9000, NOW),
    ],
)
def test_invalid_init_data_is_rejected(raw, now):
    with pytest.raises(AuthenticationError):
        verify_init_data(raw, TOKEN, now=now, max_age=300)


def test_session_is_signed_and_expires():
    codec = SessionCodec("a-long-secret-with-at-least-32-characters", max_age=30)
    user = verify_init_data(signed_init_data(), TOKEN, now=NOW)
    fingerprint = "a" * 64
    token = codec.issue(user, launch_digest=fingerprint, session_id="s" * 43)
    assert codec.verify(token, launch_digest=fingerprint).user.id == 123
    with pytest.raises(AuthenticationError):
        codec.verify(token + "tampered", launch_digest=fingerprint)
    with pytest.raises(AuthenticationError):
        codec.verify(token, launch_digest="b" * 64)
    expired = SessionCodec("a-long-secret-with-at-least-32-characters", max_age=-1)
    with pytest.raises(AuthenticationError):
        expired.verify(token, launch_digest=fingerprint)
