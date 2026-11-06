"""aiogram is the only update ingress; Wzgram is optional and outbound-only."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommandScopeAllGroupChats
from wzgram import Client

from summon._vendor import richgram_builders as richgram
from summon.api.context import Store
from summon.bot.compat import validate_runtime
from summon.bot.handlers import COMMANDS, build_router, group_commands
from summon.config import Settings
from summon.database import connect_database

logger = logging.getLogger(__name__)


def create_outbound_client(settings: Settings) -> Client:
    return Client(
        "summon-outbound",
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash.get_secret_value(),
        bot_token=settings.bot_token.get_secret_value(),
        in_memory=True,
        no_updates=True,
    )


async def register_command_menus(bot: Bot) -> None:
    await bot.set_my_commands(COMMANDS)
    scope = BotCommandScopeAllGroupChats()
    try:
        await bot.set_my_commands(group_commands(), scope=scope)
    except TelegramBadRequest:
        # Older/self-hosted Bot API servers may reject is_ephemeral; retain a usable menu.
        logger.warning("Ephemeral commands unavailable; registering a standard group menu")
        await bot.set_my_commands(COMMANDS, scope=scope)


async def run() -> None:
    settings = Settings()
    validate_runtime()
    token = settings.bot_token.get_secret_value()
    if not token:
        raise RuntimeError("SUMMON_BOT_TOKEN is required to start the bot")
    client, database = await connect_database(settings)
    bot = Bot(token)
    outbound: Client | None = None
    try:
        if settings.rich_enabled and richgram.RICH_AVAILABLE:
            candidate = create_outbound_client(settings)
            try:
                await candidate.start()
                outbound = candidate
            except Exception as exc:
                logger.warning(
                    "Outbound rich client unavailable (%s); using standard Bot API",
                    type(exc).__name__,
                )
        elif settings.rich_enabled:
            logger.warning("Installed Wzgram lacks rich-message support; using standard Bot API")
        store = Store(client, database)
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(store, settings, outbound))
        await register_command_menus(bot)
        # Polling and webhooks are mutually exclusive. Preserve pending updates.
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("SUMMON bot starting with aiogram as the sole update ingress")
        await dispatcher.start_polling(
            bot, allowed_updates=dispatcher.resolve_used_update_types(), close_bot_session=False
        )
    finally:
        if outbound is not None:
            await outbound.stop()
        await bot.session.close()
        await client.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())
