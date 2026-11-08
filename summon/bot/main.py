"""Run SUMMON's single, coordinated native Wzgram update client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from pymongo.errors import PyMongoError
from wzgram import Client, idle
from wzgram.errors import RPCError
from wzgram.types import BotCommandScopeAllGroupChats

from summon.api.context import Store
from summon.bot.compat import validate_runtime
from summon.bot.handlers import COMMANDS, group_commands, register_handlers
from summon.bot.ingress import IngressLease, IngressLeaseLost
from summon.bot.registration import validate_handler_registration
from summon.bot.session_storage import bot_id_from_token, start_verified_client, worker_storage
from summon.bot.watchdog import monitor_connection
from summon.config import Settings
from summon.database import connect_database, safe_startup_error
from summon.repositories.poller_health import PollerHealthRepository

logger = logging.getLogger(__name__)


class EmbeddedBotShutdownUncertain(RuntimeError):
    """The embedded worker must not restart while its previous client may be alive."""


async def _embedded_idle() -> None:
    await asyncio.Event().wait()


async def register_command_menus(bot: Client) -> None:
    try:
        await bot.set_bot_commands(COMMANDS)
        await bot.set_bot_commands(group_commands(), scope=BotCommandScopeAllGroupChats())
    except RPCError as exc:
        # Menu registration is nonessential; command handlers remain available.
        logger.warning("Telegram command menu registration failed (%s)", type(exc).__name__)


async def run(
    *,
    settings: Settings | None = None,
    shared_database: tuple[Any, Any] | None = None,
    on_started: Callable[[], None] | None = None,
    embedded: bool = False,
) -> None:
    settings = settings or Settings()
    validate_runtime()
    token = settings.bot_token.get_secret_value()
    bot_id = bot_id_from_token(token)
    if not settings.telegram_api_id or not settings.telegram_api_hash.get_secret_value():
        raise RuntimeError("SUMMON_TELEGRAM_API_ID and SUMMON_TELEGRAM_API_HASH are required")
    if not settings.webhooks_disabled_confirmed:
        raise RuntimeError(
            "Stop other update consumers and remove any existing Bot API webhook before "
            "setting SUMMON_WEBHOOKS_DISABLED_CONFIRMED=true"
        )

    owns_mongo = shared_database is None
    if shared_database is None:
        try:
            mongo, database = await connect_database(settings)
        except PyMongoError as exc:
            raise safe_startup_error(exc) from None
    else:
        # The API has already checked topology and initialized schema. Reuse
        # its client so embedded bot restarts never repeat those writes.
        mongo, database = shared_database
    lease = IngressLease(database, bot_id)
    bot: Client | None = None
    lease_task: asyncio.Task[None] | None = None
    monitor_task: asyncio.Task[None] | None = None
    idle_task: asyncio.Task[None] | None = None
    startup_task: asyncio.Task[None] | None = None
    acquired = False
    try:
        async with asyncio.timeout(10):
            await lease.acquire()
        acquired = True
        # Renew during Telegram login and gap recovery as well as normal operation.
        lease_task = asyncio.create_task(lease.maintain(), name="ingress-lease")
        storage = worker_storage(mongo, settings.telegram_session_database)
        bot = Client(
            "summon-bot",
            api_id=settings.telegram_api_id,
            api_hash=settings.telegram_api_hash.get_secret_value(),
            bot_token=token,
            storage_engine=storage,
            no_updates=False,
            skip_updates=False,
            auto_no_updates=False,
        )
        lease.register(bot)
        register_handlers(bot, Store(mongo, database), settings)
        # Wzgram schedules registration on the current event loop. Let the
        # registration tasks finish and verify them BEFORE connecting Telegram.
        await asyncio.sleep(0)
        validate_handler_registration(bot)

        async def start_bot() -> None:
            if bot is None:
                raise RuntimeError("Wzgram client was not created")
            async with asyncio.timeout(60):
                await start_verified_client(bot, token, bot_id)
            if not lease.valid():
                raise IngressLeaseLost("Update-worker lease expired during startup")
            try:
                async with asyncio.timeout(10):
                    await register_command_menus(bot)
            except TimeoutError:
                logger.warning("Telegram command menu registration timed out")

        startup_task = asyncio.create_task(start_bot(), name="telegram-startup")
        startup_done, _ = await asyncio.wait(
            {startup_task, lease_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if lease_task in startup_done:
            await lease_task
            raise IngressLeaseLost("Update-worker lease stopped during startup")
        await startup_task
        logger.info("SUMMON started with one native Wzgram update consumer")
        health = (
            PollerHealthRepository(database, token, settings.session_secret.get_secret_value())
            if settings.poller_heartbeat_enabled
            else None
        )
        monitor_task = asyncio.create_task(
            monitor_connection(
                bot,
                lease,
                timeout_seconds=settings.poll_watchdog_seconds,
                health_repository=health,
            ),
            name="telegram-connection-monitor",
        )
        # Wzgram's idle() installs process signal handlers. Uvicorn owns those
        # in embedded mode, so wait for task cancellation instead.
        idle_wait = _embedded_idle() if embedded else idle()
        idle_task = asyncio.create_task(idle_wait, name="telegram-idle")
        if on_started is not None:
            on_started()
        done, _ = await asyncio.wait(
            {lease_task, monitor_task, idle_task}, return_when=asyncio.FIRST_COMPLETED
        )
        # A lost lease or failed connection monitor causes the process manager
        # to restart this worker; a signal normally resolves idle().
        for finished in done:
            await finished
    finally:
        lease.invalidate()
        for pending_task in (startup_task, idle_task, monitor_task, lease_task):
            if pending_task is not None:
                pending_task.cancel()
        await asyncio.gather(
            *(
                task
                for task in (startup_task, idle_task, monitor_task, lease_task)
                if task is not None
            ),
            return_exceptions=True,
        )
        stopped = bot is None
        if bot is not None:
            try:
                async with asyncio.timeout(20):
                    await bot.stop()
                stopped = not bot.is_connected and not bot.is_initialized
            except Exception as exc:
                logger.error("Could not stop Wzgram cleanly (%s)", type(exc).__name__)
        if acquired and stopped:
            try:
                async with asyncio.timeout(5):
                    await lease.release()
            except Exception as exc:
                logger.warning("Could not release ingress lease (%s)", type(exc).__name__)
        # If disconnect was uncertain, leave the lease to expire rather than
        # deliberately allowing a second worker to start immediately.
        if owns_mongo:
            await mongo.close()
        if embedded and not stopped:
            # Unlike a separate process, a failed embedded client may still
            # hold a socket. Do not start another client in this process.
            raise EmbeddedBotShutdownUncertain("Wzgram shutdown was not confirmed") from None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())
