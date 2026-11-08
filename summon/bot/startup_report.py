"""Send a minimal, best-effort startup report to the configured owner."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime

from wzgram import Client

from summon.bot.ingress import IngressLease
from summon.bot.presentation import Card, Emoji, send_card
from summon.config import Settings

logger = logging.getLogger(__name__)
_STARTUP_DM_TIMEOUT_SECONDS = 10


async def notify_owner_startup(bot: Client, settings: Settings, lease: IngressLease) -> None:
    """Attempt one owner DM after worker startup without blocking the worker.

    Visual fallbacks may be attempted after a definite Telegram rejection by
    send_card, but ambiguous failures or unconfirmed sends are never retried.
    """
    if not settings.startup_dm_enabled or settings.owner_id is None:
        return
    try:
        if not lease.valid():
            return
        username = getattr(getattr(bot, "me", None), "username", None)
        fields = [
            ("Worker", "Native Wzgram update consumer"),
            ("Database", "Initialization completed"),
            ("Started", datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")),
        ]
        if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{1,32}", username):
            fields.insert(0, ("Bot", f"@{username}"))
        card = Card(
            "SUMMON is online",
            "The bot worker started. Check Telegram commands to verify update delivery.",
            emoji=Emoji.SUMMON_BRAND,
            fields=tuple(fields),
        )
        async with asyncio.timeout(_STARTUP_DM_TIMEOUT_SECONDS):
            result = await send_card(
                bot,
                settings.owner_id,
                card,
                rich=settings.rich_enabled and settings.bot_api_rich_enabled,
                premium=settings.premium_emoji_enabled,
            )
        if result is None:
            logger.warning("Owner startup notification delivery was not confirmed; not retrying.")
    except TimeoutError:
        logger.warning("Owner startup notification timed out; delivery is unconfirmed.")
    except Exception as exc:
        # Never log the recipient ID or error message; either may be private.
        logger.warning("Owner startup notification failed (%s).", type(exc).__name__)
