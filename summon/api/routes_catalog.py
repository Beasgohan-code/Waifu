"""API routes for a single SUMMON domain."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pymongo.errors import PyMongoError

from summon.api.dependencies import get_current_user, get_store, limit_action
from summon.api.schemas import (
    CharacterID,
    Toggle,
)
from summon.auth import TelegramUser
from summon.models import InventorySort, Rarity
from summon.repositories.character_numbers import CharacterNumberingUnavailable
from summon.repositories.collection_insights import InsightsDataError
from summon.repositories.inventory_paging import (
    CursorDataError,
    CursorExpired,
    CursorInvalid,
    CursorUnavailable,
)

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


@router.get("/artwork/{digest}", response_class=Response)
async def artwork(
    digest: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")],
    request: Request,
    store: Any = Depends(get_store),
) -> Response:
    """Public immutable image data for the Mini App and Telegram's photo fetcher."""
    lookup = getattr(store.catalog, "artwork", None)
    if not callable(lookup):
        raise HTTPException(404, "Artwork not found")
    limiter = getattr(store, "limiter", None)
    if limiter is None:
        raise HTTPException(503, "Artwork is temporarily unavailable")
    remote = request.client.host if request.client else "unknown"
    # Apply the bound before loading or hashing image bytes, including misses.
    # Do not trust browser-supplied forwarding headers. Shared Vercel egress can
    # aggregate clients; public caching reduces the chance of hitting this cap.
    if not await limiter.allow(f"artwork:{remote}", limit=600, seconds=60):
        raise HTTPException(429, "Too many artwork requests")
    content = await lookup(digest)
    if not content:
        raise HTTPException(404, "Artwork not found")
    return Response(content=content, media_type="image/jpeg")


@router.get("/characters")
async def characters(
    search: str = Query(default="", max_length=80),
    rarity: Rarity | None = None,
    category: str | None = Query(default=None, max_length=120),
    page: int = Query(default=1, ge=1, le=25),
    limit: int = Query(default=24, ge=1, le=50),
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    return {
        "data": await store.catalog.list(
            user.id, search=search, rarity=rarity, category=category, page=page, limit=limit
        )
    }


@router.get("/characters/{character_id}")
async def character_detail(
    character_id: CharacterID,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    try:
        character = await store.catalog.details(user.id, character_id)
    except CharacterNumberingUnavailable:
        raise HTTPException(503, "Character numbering requires operator review") from None
    if not character:
        raise HTTPException(404, "Character not found")
    return {"data": character}


@router.get("/inventory")
async def inventory(
    page: int = Query(default=1, ge=1, le=25),
    limit: int = Query(default=24, ge=1, le=50),
    favorite: bool = False,
    duplicates: bool = False,
    sort: InventorySort = "recent",
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    return {
        "data": await store.catalog.inventory(
            user.id, page=page, limit=limit, favorite=favorite, duplicates=duplicates, sort=sort
        )
    }


@router.get("/inventory/pages")
async def inventory_pages(
    request: Request,
    cursor: str | None = Query(default=None, min_length=1, max_length=2048),
    limit: int = Query(default=24, ge=1, le=50),
    favorite: bool = False,
    duplicates: bool = False,
    sort: InventorySort = "recent",
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    settings = request.app.state.settings
    if not settings.preview_mode:
        await limit_action(request, user, "inventory-pages", limit=120)
    secret = settings.session_secret.get_secret_value()
    if settings.preview_mode and not secret:
        # The read-only preview has an ephemeral key, never a production credential.
        secret = request.app.state.preview_cursor_secret
    try:
        result = await store.catalog.inventory_cursor(
            user.id,
            secret=secret,
            cursor=cursor,
            limit=limit,
            favorite=favorite,
            duplicates=duplicates,
            sort=sort,
        )
    except CursorInvalid as exc:
        raise HTTPException(400, "Invalid collection cursor; refresh the collection") from exc
    except CursorExpired as exc:
        raise HTTPException(409, "Collection snapshot expired; refresh the collection") from exc
    except CursorDataError as exc:
        raise HTTPException(
            503, "Collection data needs repair; use standard pages or contact support"
        ) from exc
    except CursorUnavailable as exc:
        raise HTTPException(
            503, "Stable collection pages are unavailable; use standard pages"
        ) from exc
    return {"data": result}


@router.put("/characters/{character_id}/favorite")
async def favorite(
    character_id: CharacterID,
    body: Toggle,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    await limit_action(request, user, "favorite")
    if not await store.catalog.set_favorite(user.id, character_id, body.value):
        raise HTTPException(404, "Character is not in your collection")
    return {"data": {"favorite": body.value}}


@router.put("/characters/{character_id}/wishlist")
async def wishlist(
    character_id: CharacterID,
    body: Toggle,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    await limit_action(request, user, "wishlist")
    if not await store.catalog.set_wishlist(user.id, character_id, body.value):
        raise HTTPException(404, "Character not found")
    return {"data": {"wishlisted": body.value}}


@router.post("/daily")
async def daily(
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    await limit_action(request, user, "daily", limit=10)
    return {"data": await store.users.daily(user, datetime.now(UTC))}


@router.get("/me/collection-insights")
async def collection_insights(
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    """Only the session's actor can read their own collection statistics."""
    if not request.app.state.settings.preview_mode:
        await limit_action(request, user, "collection-insights", limit=30)
    try:
        return {"data": await store.users.collection_insights(user.id)}
    except (InsightsDataError, PyMongoError, TimeoutError) as exc:
        logger.warning("Collection insights unavailable (%s)", type(exc).__name__)
        raise HTTPException(503, "Collection insights are temporarily unavailable") from None


@router.get("/progression")
async def progression(
    user: TelegramUser = Depends(get_current_user), store: Any = Depends(get_store)
) -> dict[str, Any]:
    return {"data": await store.users.progression(user.id, datetime.now(UTC))}


@router.get("/leaderboard")
async def leaderboard(
    user: TelegramUser = Depends(get_current_user), store: Any = Depends(get_store)
) -> dict[str, Any]:
    return {"data": await store.users.leaderboard()}
