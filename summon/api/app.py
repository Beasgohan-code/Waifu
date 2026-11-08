"""SUMMON HTTP API: signed Telegram sessions and same-origin boundaries."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pymongo.errors import PyMongoError
from wzgram import Client
from wzgram.errors import RPCError

from summon.api.context import Store
from summon.api.dependencies import get_current_session, get_current_user, get_store
from summon.api.session_cookies import clear_session_cookies, set_session_cookie
from summon.api.static import PublicWebFiles
from summon.auth import (
    AuthenticationError,
    BoundSession,
    SessionCodec,
    TelegramUser,
    launch_fingerprint,
    verify_init_data,
)
from summon.bot.emoji_palette import palette_icon
from summon.bot.session_storage import APIStorage, bot_id_from_token, start_verified_client
from summon.bot.supervisor import WorkerStatus, supervise_embedded_worker
from summon.config import Settings
from summon.database import (
    DatabaseTopologyError,
    connect_database,
    require_transaction_topology,
    safe_startup_error,
)
from summon.repositories.poller_health import PollerHealthRepository

logger = logging.getLogger(__name__)


class SessionRequest(BaseModel):
    init_data: str = Field(min_length=1, max_length=8192)


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


def create_app(
    *, settings: Settings | None = None, store: Any | None = None, telegram_bot: Any | None = None
) -> FastAPI:
    settings = settings or Settings()
    monitor_token = settings.poller_monitor_token.get_secret_value()
    if settings.poller_heartbeat_enabled and (
        not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", monitor_token)
        or monitor_token == settings.bot_token.get_secret_value()
        or monitor_token == settings.session_secret.get_secret_value()
    ):
        raise ValueError("SUMMON_POLLER_MONITOR_TOKEN must be a separate URL-safe secret")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = None
        owned_bot = None
        candidate = None
        startup_task: asyncio.Task[None] | None = None
        worker_task: asyncio.Task[None] | None = None
        embedded_worker = settings.run_bot_worker and not settings.preview_mode and store is None
        worker_status = WorkerStatus("waiting_for_database" if embedded_worker else "disabled")
        app.state.settings = settings
        app.state.store = None
        app.state.database = None
        app.state.telegram_bot = telegram_bot
        app.state.startup_task = None
        app.state.worker_task = None
        app.state.worker_status = worker_status

        async def initialize_runtime() -> None:
            nonlocal client, owned_bot, candidate, worker_task
            try:
                # Only the read-only ping continues in the background; topology,
                # schema and index initialization are still performed once.
                client, database = await connect_database(settings, wait_until_available=True)
                initialized_store = Store(client, database)
                app.state.database = database
                app.state.store = initialized_store
                logger.info("MongoDB initialized; API database operations are available")
                if embedded_worker:
                    worker_task = asyncio.create_task(
                        supervise_embedded_worker(settings, client, database, worker_status),
                        name="summon-embedded-bot",
                    )
                    app.state.worker_task = worker_task

                    def report_worker_failure(task: asyncio.Task[None]) -> None:
                        if not task.cancelled() and (failure := task.exception()) is not None:
                            worker_status.state = "failed"
                            worker_status.reason = "supervisor_failure"
                            logger.error("Bot supervision failed (%s)", type(failure).__name__)

                    worker_task.add_done_callback(report_worker_failure)
            except PyMongoError as exc:
                # Never log the driver exception: it may contain hosts or credentials.
                if embedded_worker:
                    worker_status.state = "database_unavailable"
                logger.error("%s", safe_startup_error(exc))
                return
            except DatabaseTopologyError:
                if embedded_worker:
                    worker_status.state = "database_unavailable"
                logger.error("MongoDB is not a supported writable transaction topology")
                return
            except Exception as exc:
                if embedded_worker:
                    worker_status.state = "database_unavailable"
                logger.error("API database initialization failed (%s)", type(exc).__name__)
                return
            finally:
                if client is not None and app.state.store is None:
                    try:
                        await client.close()
                    except Exception as exc:
                        logger.warning("MongoDB cleanup failed (%s)", type(exc).__name__)
                    finally:
                        client = None

            if (
                telegram_bot is not None
                or not settings.bot_token.get_secret_value()
                or not settings.telegram_api_id
                or not settings.telegram_api_hash.get_secret_value()
            ):
                return
            # The API session receives no updates and never replaces the single
            # coordinated bot worker. Only peer records are shared with it.
            connected_client = client
            if connected_client is None:
                return
            token = settings.bot_token.get_secret_value()
            try:
                candidate = Client(
                    "summon-api",
                    api_id=settings.telegram_api_id,
                    api_hash=settings.telegram_api_hash.get_secret_value(),
                    bot_token=token,
                    storage_engine=APIStorage(connected_client[settings.telegram_session_database]),
                    no_updates=True,
                )
                async with asyncio.timeout(30):
                    await start_verified_client(candidate, token, bot_id_from_token(token))
            except Exception as exc:
                logger.warning("Telegram group management unavailable (%s)", type(exc).__name__)
                if candidate is not None:
                    try:
                        await candidate.stop()
                    except Exception as stop_exc:
                        logger.warning("Telegram cleanup failed (%s)", type(stop_exc).__name__)
                    candidate = None
            else:
                owned_bot = candidate
                app.state.telegram_bot = candidate
                candidate = None

        try:
            if settings.preview_mode:
                app.state.preview_cursor_secret = secrets.token_urlsafe(32)
            if store is not None:
                app.state.store = store
            elif settings.preview_mode:
                from summon.api.preview import PreviewStore

                app.state.store = PreviewStore()
            else:
                # Yield immediately so Uvicorn can bind PORT and serve /health;
                # database-backed requests remain unavailable until initialized.
                startup_task = asyncio.create_task(initialize_runtime())
                app.state.startup_task = startup_task
            yield
        finally:
            if startup_task is not None:
                startup_task.cancel()
                await asyncio.gather(startup_task, return_exceptions=True)
            if worker_task is not None:
                worker_status.state = "stopping"
                worker_status.reason = None
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
                worker_status.state = "stopped"
            try:
                if candidate is not None:
                    await candidate.stop()
                if owned_bot is not None:
                    await owned_bot.stop()
            finally:
                if client is not None:
                    await client.close()

    app = FastAPI(title="SUMMON API", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def security_and_logging(request: Request, call_next: Any) -> Response:
        started = time.monotonic()
        if request.url.path.startswith("/api/") and request.method in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            if settings.preview_mode:
                return error_response(403, "preview_read_only", "Preview mode is read-only")
            if request.headers.get("X-Summon-Request") != "1":
                return error_response(403, "csrf", "Missing request verification header")
            origin = request.headers.get("origin")
            same_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin and origin != same_origin and origin not in settings.allowed_origins:
                return error_response(403, "csrf", "Cross-origin request rejected")
        if (
            request.url.path.startswith("/api/")
            and request.url.path != "/api/bootstrap"
            and getattr(request.app.state, "store", None) is None
        ):
            return error_response(503, "database_unavailable", "Database is not ready; try again")
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if (
            request.url.path.startswith("/api/artwork/")
            and request.method == "GET"
            and response.status_code == 200
        ):
            # Content-addressed public artwork is immutable. Avoid making every
            # catalog view re-download multi-megabyte photos from MongoDB.
            response.headers["Cache-Control"] = "public, max-age=3600, immutable"
        elif (
            request.url.path == "/"
            or request.url.path.startswith("/api/")
            or request.url.path.startswith("/health")
        ):
            response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        logger.info(
            "request method=%s route=%s status=%s duration_ms=%d",
            request.method,
            getattr(route, "path", "unmatched"),
            response.status_code,
            int((time.monotonic() - started) * 1000),
        )
        return response

    # CORS must wrap early 503/CSRF responses as well as normal routes; browser
    # clients on an allowed origin should be able to read the actual error.
    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "X-Summon-Request", "X-Summon-Launch"],
        )

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return error_response(exc.status_code, "request_error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return error_response(
            422, "invalid_input", "The request contains invalid or missing fields"
        )

    @app.exception_handler(DatabaseTopologyError)
    async def database_topology_error(
        _request: Request, _exc: DatabaseTopologyError
    ) -> JSONResponse:
        return error_response(
            503, "database_topology_unsupported", "The database is not ready for transactions"
        )

    @app.exception_handler(PyMongoError)
    async def database_error(_request: Request, exc: PyMongoError) -> JSONResponse:
        logger.error("Database operation failed: %s", type(exc).__name__)
        return error_response(
            503, "database_unavailable", "The database is unavailable; please try again"
        )

    @app.exception_handler(RPCError)
    @app.exception_handler(ConnectionError)
    @app.exception_handler(TimeoutError)
    async def telegram_error(_request: Request, exc: Exception) -> JSONResponse:
        logger.warning("Telegram operation unavailable (%s)", type(exc).__name__)
        return error_response(
            503, "telegram_unavailable", "Could not verify current Telegram access"
        )

    from summon.auto_replies import AutoReplyConflict, AutoReplyStorageError
    from summon.group_notes import NoteConflict, NoteStorageError
    from summon.repositories.users import DailyAlreadyClaimed, DailyStatusUnavailable
    from summon.services.gifting import GiftConflict, GiftForbidden, GiftNotFound
    from summon.services.moderation import PermissionDenied
    from summon.services.trading import TradeConflict, TradeForbidden, TradeNotFound

    @app.exception_handler(PermissionDenied)
    @app.exception_handler(TradeForbidden)
    @app.exception_handler(GiftForbidden)
    async def permission_error(_request: Request, exc: Exception) -> JSONResponse:
        return error_response(403, "forbidden", str(exc))

    @app.exception_handler(TradeConflict)
    @app.exception_handler(GiftConflict)
    @app.exception_handler(DailyAlreadyClaimed)
    @app.exception_handler(NoteConflict)
    @app.exception_handler(AutoReplyConflict)
    async def conflict_error(_request: Request, exc: Exception) -> JSONResponse:
        return error_response(409, "conflict", str(exc))

    @app.exception_handler(DailyStatusUnavailable)
    async def daily_status_error(_request: Request, _exc: DailyStatusUnavailable) -> JSONResponse:
        return error_response(
            503, "daily_unavailable", "Daily status is unavailable; please contact support"
        )

    @app.exception_handler(NoteStorageError)
    async def note_storage_error(_request: Request, _exc: NoteStorageError) -> JSONResponse:
        return error_response(503, "notes_unavailable", "Stored notes need operator review")

    @app.exception_handler(AutoReplyStorageError)
    async def auto_reply_storage_error(
        _request: Request, _exc: AutoReplyStorageError
    ) -> JSONResponse:
        return error_response(
            503, "auto_replies_unavailable", "Stored auto replies need operator review"
        )

    @app.exception_handler(TradeNotFound)
    @app.exception_handler(GiftNotFound)
    @app.exception_handler(LookupError)
    async def not_found_error(_request: Request, _exc: Exception) -> JSONResponse:
        return error_response(404, "not_found", "The requested resource was not found")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "summon"}

    @app.get("/health/ready")
    async def readiness(request: Request) -> dict[str, str]:
        if settings.preview_mode:
            return {"status": "preview"}
        if request.app.state.store is None:
            raise HTTPException(503, "Database initialization has not completed")
        database = request.app.state.database
        if database is None:
            return {"status": "injected"}
        try:
            # Keep the readiness response bounded even if the database stalls.
            async with asyncio.timeout(4):
                await database.command("ping")
                await require_transaction_topology(database.client)
        except TimeoutError:
            raise HTTPException(503, "Database readiness check timed out") from None
        return {"status": "ready"}

    @app.get("/health/bot")
    async def bot_health(request: Request) -> JSONResponse:
        worker = getattr(request.app.state, "worker_status", WorkerStatus())
        # Running means this process completed native worker startup, not that
        # Telegram delivered an update or every handler is responsive. Reasons
        # are fixed codes, never exception messages or credentials.
        content = {"status": worker.state}
        if worker.reason is not None:
            content["reason"] = worker.reason
        return JSONResponse(status_code=200 if worker.state == "running" else 503, content=content)

    @app.get("/health/poller", include_in_schema=False)
    async def poller_health(request: Request) -> JSONResponse:
        candidates = request.headers.getlist("X-Summon-Monitor-Token")
        # Authenticate before any DB access. Never accept a token in the URL.
        if (
            not settings.poller_heartbeat_enabled
            or len(candidates) != 1
            or len(candidates[0]) > 128
            or not secrets.compare_digest(
                candidates[0].encode("utf-8"), monitor_token.encode("utf-8")
            )
        ):
            raise HTTPException(404, "Not Found")
        database = getattr(request.app.state, "database", None)
        if settings.preview_mode or store is not None or database is None:
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        try:
            repository = PollerHealthRepository(
                database,
                settings.bot_token.get_secret_value(),
                settings.session_secret.get_secret_value(),
            )
            async with asyncio.timeout(5):
                recent = await repository.is_recent(settings.poller_status_max_age_seconds)
        except Exception as exc:
            # Monitoring failures never become an apparently healthy result,
            # and underlying database errors may contain sensitive details.
            logger.warning("Connection health lookup failed (%s)", type(exc).__name__)
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return JSONResponse(
            status_code=200 if recent else 503,
            content={"status": "recent_connection" if recent else "no_recent_connection"},
        )

    @app.get("/api/bootstrap")
    async def bootstrap() -> dict[str, Any]:
        fallback, custom_emoji_id = palette_icon("summon_bot", 60)
        return {
            "data": {
                "preview": settings.preview_mode,
                "collection_button_emoji": {
                    "fallback": fallback,
                    "custom_emoji_id": custom_emoji_id if settings.premium_emoji_enabled else None,
                },
            }
        }

    @app.post("/api/auth/session")
    async def create_session(
        body: SessionRequest,
        request: Request,
        response: Response,
        current_store: Any = Depends(get_store),
    ) -> dict[str, Any]:
        remote = request.client.host if request.client else "unknown"
        if not await current_store.limiter.allow(f"auth:{remote}", limit=20, seconds=60):
            raise HTTPException(429, "Too many sign-in attempts")
        token = settings.bot_token.get_secret_value()
        secret = settings.session_secret.get_secret_value()
        if not token or len(secret) < 32:
            raise HTTPException(503, "Telegram sign-in is not configured")
        try:
            user = verify_init_data(body.init_data, token, max_age=settings.init_data_max_age)
        except AuthenticationError as exc:
            raise HTTPException(401, "Invalid or expired Telegram launch data") from exc
        now = datetime.now(UTC)
        proof = parse_qs(body.init_data)["hash"][0]
        fingerprint = launch_fingerprint(proof)
        if request.headers.get("X-Summon-Launch") != fingerprint:
            raise HTTPException(401, "Telegram launch data does not match this session")
        if not await current_store.auth.consume(proof, now, settings.init_data_max_age):
            raise HTTPException(401, "Telegram launch data has already been used; reopen SUMMON")
        await current_store.users.touch(user, now)
        session_id = secrets.token_urlsafe(32)
        signed_cookie = SessionCodec(secret, max_age=settings.session_max_age).issue(
            user, launch_digest=fingerprint, session_id=session_id
        )
        await current_store.sessions.create(session_id, user.id, now, settings.session_max_age)
        set_session_cookie(
            response,
            signed_cookie,
            max_age=settings.session_max_age,
            production=settings.environment == "production",
        )
        return {"data": {"first_name": user.first_name}}

    @app.post("/api/auth/logout")
    async def logout(
        response: Response,
        session: BoundSession = Depends(get_current_session),
        current_store: Any = Depends(get_store),
    ) -> dict[str, Any]:
        await current_store.sessions.revoke(session.session_id, session.user.id)
        clear_session_cookies(response, production=settings.environment == "production")
        return {"data": {"signed_out": True}}

    @app.get("/api/me")
    async def me(
        user: TelegramUser = Depends(get_current_user), current_store: Any = Depends(get_store)
    ) -> dict[str, Any]:
        profile = await current_store.users.profile(user.id)
        profile["is_operator"] = user.id in settings.operator_ids
        return {"data": profile}

    from summon.api.routes import router

    app.include_router(router)
    if settings.web_dist:
        # Only the explicitly configured build directory is public. API and
        # health routes above take precedence, including for unknown API paths.
        app.mount("/", PublicWebFiles(directory=settings.web_dist, html=True), name="web")
    else:

        @app.get("/", include_in_schema=False)
        async def backend_index(request: Request) -> dict[str, str]:
            worker = getattr(request.app.state, "worker_status", WorkerStatus())
            result = {
                "service": "SUMMON bot and API backend",
                "bot_status": worker.state,
                "health": "/health",
                "database_readiness": "/health/ready",
                "bot_health": "/health/bot",
            }
            if worker.reason is not None:
                result["bot_reason"] = worker.reason
            # Render exposes the deployed Git SHA as a non-secret system env.
            # Publish only a valid SHA, never an arbitrary environment value.
            revision = os.environ.get("RENDER_GIT_COMMIT", "")
            if re.fullmatch(r"[0-9a-f]{40}", revision):
                result["revision"] = revision
            return result

    return app


app = create_app()
