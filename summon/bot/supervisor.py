"""Supervise one native update worker alongside the HTTP API.

The API owns MongoDB initialization and the process signal handlers. The worker
uses the same async Mongo client but a separate persistent Wzgram session; its
existing ingress lease still coordinates competing processes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from wzgram.errors import BadRequest, FloodWait, Forbidden, Unauthorized

from summon.bot.ingress import IngressLeaseLost
from summon.bot.main import EmbeddedBotShutdownUncertain, run
from summon.bot.session_storage import bot_id_from_token
from summon.config import Settings

logger = logging.getLogger(__name__)
_RETRY_DELAYS = (5, 10, 30, 60, 120, 300)


@dataclass
class WorkerStatus:
    # This is process-local startup state, not proof of delivery or liveness.
    state: str = "disabled"
    # Fixed, public reason codes only; never include exception messages or IDs.
    reason: str | None = None


async def _wait_before_retry(seconds: int) -> None:
    await asyncio.sleep(seconds)


async def supervise_embedded_worker(
    settings: Settings, mongo: Any, database: Any, status: WorkerStatus
) -> None:
    if not settings.webhooks_disabled_confirmed:
        status.state = "configuration_required"
        status.reason = "webhook_confirmation_required"
        logger.error(
            "Bot worker is stopped: after stopping other update consumers and removing "
            "any Bot API webhook, set WEBHOOKS_DISABLED_CONFIRMED=true."
        )
        return
    token = settings.bot_token.get_secret_value()
    if (
        not token
        or not settings.telegram_api_id
        or not settings.telegram_api_hash.get_secret_value()
    ):
        status.state = "configuration_required"
        status.reason = "telegram_credentials_required"
        logger.error("Bot worker requires BOT_TOKEN, API_ID and API_HASH.")
        return
    try:
        bot_id_from_token(token)
    except RuntimeError:
        status.state = "configuration_required"
        status.reason = "bot_token_invalid_format"
        logger.error("Bot worker requires a valid BOT_TOKEN.")
        return

    failures = 0
    loop = asyncio.get_running_loop()
    while True:
        status.state = "starting"
        status.reason = None
        started_at: float | None = None

        def started() -> None:
            nonlocal started_at
            started_at = loop.time()
            status.state = "running"
            status.reason = None
            logging.getLogger("uvicorn.error").info(
                "Native Telegram bot worker started; delivery is not yet verified."
            )

        failure: Exception | None = None
        try:
            await run(
                settings=settings,
                shared_database=(mongo, database),
                on_started=started,
                embedded=True,
            )
        except asyncio.CancelledError:
            raise
        except EmbeddedBotShutdownUncertain:
            status.state = "restart_required"
            status.reason = "shutdown_unconfirmed"
            logger.error(
                "Bot shutdown was not confirmed; restart this service before starting "
                "another embedded update worker."
            )
            return
        except (BadRequest, Forbidden, Unauthorized) as exc:
            status.state = "configuration_required"
            status.reason = "telegram_startup_rejected"
            logger.error(
                "Telegram rejected bot startup or session (%s); operator review required.",
                type(exc).__name__,
            )
            return
        except Exception as exc:
            # Runtime errors before startup may describe a mismatched stored
            # session or invalid configuration; do not repeatedly authenticate.
            if (
                started_at is None
                and isinstance(exc, (RuntimeError, ValueError))
                and not isinstance(exc, IngressLeaseLost)
            ):
                status.state = "configuration_required"
                status.reason = "worker_configuration_or_session"
                logger.error("Bot startup needs operator review (%s).", type(exc).__name__)
                return
            failure = exc

        if started_at is not None and loop.time() - started_at >= 60:
            failures = 0
        delay = _RETRY_DELAYS[min(failures, len(_RETRY_DELAYS) - 1)]
        failures += 1
        if isinstance(failure, FloodWait) and isinstance(failure.value, int):
            delay = max(delay, failure.value)
        status.state = "retrying"
        if isinstance(failure, FloodWait):
            status.reason = "telegram_rate_limit"
        elif isinstance(failure, IngressLeaseLost):
            status.reason = "lease_unavailable"
        else:
            status.reason = "transient_failure"
        # Never echo exception messages: Telegram and database errors may
        # contain credentials, hosts or other sensitive details.
        logger.warning(
            "Bot worker stopped (%s); retrying in %d seconds.",
            type(failure).__name__ if failure is not None else "unexpected exit",
            delay,
        )
        await _wait_before_retry(delay)
