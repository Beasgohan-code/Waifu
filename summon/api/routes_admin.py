"""API routes for a single SUMMON domain."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from summon.api.dependencies import get_current_user, get_store, limit_action
from summon.api.schemas import (
    CatalogReviewCommit,
    CharacterID,
)
from summon.auth import TelegramUser
from summon.config import Settings
from summon.models import CharacterInput
from summon.repositories.catalog import (
    CatalogConflict,
    CatalogRevisionUnavailable,
    CatalogStaleRevision,
)
from summon.repositories.character_numbers import CharacterNumberingUnavailable

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


@router.put("/admin/characters/{character_id}")
async def save_character(
    character_id: CharacterID,
    body: CharacterInput,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    if user.id not in request.app.state.settings.operator_ids:
        raise HTTPException(403, "Catalog operator access required")
    if character_id != body.id:
        raise HTTPException(400, "Character IDs do not match")
    await limit_action(request, user, "catalog", limit=20)
    try:
        result = await store.catalog.upsert_character(body, actor_id=user.id)
    except (CatalogConflict, CharacterNumberingUnavailable):
        raise HTTPException(
            409, "Catalog numbering conflicts; operator review is required"
        ) from None
    logger.info("catalog_changed actor=%s character=%s", user.id, character_id)
    return {"data": result}


def _revision_secret(request: Request) -> str:
    settings: Settings = request.app.state.settings
    secret = settings.session_secret.get_secret_value()
    if settings.preview_mode and not secret:
        secret = request.app.state.preview_cursor_secret
    if len(secret) < 32:
        raise HTTPException(503, "Catalog review is temporarily unavailable")
    return secret


@router.get("/admin/characters/{character_id}/edit-state")
async def catalog_edit_state(
    character_id: CharacterID,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    if user.id not in request.app.state.settings.operator_ids:
        raise HTTPException(403, "Catalog operator access required")
    if not request.app.state.settings.preview_mode:
        await limit_action(request, user, "catalog-review", limit=60)
    try:
        state = await store.catalog.edit_state(character_id, secret=_revision_secret(request))
    except CatalogRevisionUnavailable:
        raise HTTPException(503, "Catalog record needs operator review") from None
    return {"data": state}


@router.put("/admin/characters/{character_id}/reviewed")
async def save_reviewed_character(
    character_id: CharacterID,
    body: CatalogReviewCommit,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    if user.id not in request.app.state.settings.operator_ids:
        raise HTTPException(403, "Catalog operator access required")
    if character_id != body.character.id:
        raise HTTPException(400, "Character IDs do not match")
    await limit_action(request, user, "catalog", limit=20)
    try:
        result = await store.catalog.upsert_character(
            body.character,
            actor_id=user.id,
            expected_revision=body.expected_revision,
            revision_secret=_revision_secret(request),
        )
    except CatalogStaleRevision:
        raise HTTPException(409, "Catalog entry changed; review the latest version") from None
    except (CatalogConflict, CharacterNumberingUnavailable):
        raise HTTPException(409, "Catalog numbering requires operator review") from None
    except CatalogRevisionUnavailable:
        raise HTTPException(503, "Catalog record needs operator review") from None
    logger.info("catalog_reviewed_change actor=%s character=%s", user.id, character_id)
    return {"data": result}
