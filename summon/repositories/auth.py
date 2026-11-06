"""One-time use of Telegram launch proofs across API workers."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pymongo.errors import DuplicateKeyError
from pymongo.write_concern import WriteConcern

from summon.auth import TELEGRAM_FUTURE_TOLERANCE_SECONDS, launch_fingerprint


class AuthRepository:
    def __init__(self, database: Any) -> None:
        # Prevent an acknowledged nonce from being rolled back on normal failover.
        self.nonces = database.auth_nonces.with_options(
            write_concern=WriteConcern(w="majority", wtimeout=5000)
        )

    async def consume(self, proof: str, now: datetime, max_age: int) -> bool:
        try:
            await self.nonces.insert_one(
                {
                    "_id": launch_fingerprint(proof),
                    # Retain through future tolerance and the final accepted
                    # integer second, with a buffer for modest clock skew.
                    "expires_at": now
                    + timedelta(seconds=max_age + TELEGRAM_FUTURE_TOLERANCE_SECONDS + 60),
                }
            )
            return True
        except DuplicateKeyError:
            return False
