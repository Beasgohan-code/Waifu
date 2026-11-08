"""API routes for a single SUMMON domain."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from summon.api.dependencies import get_current_user, get_store, limit_action
from summon.api.schemas import (
    CharacterID,
)
from summon.auth import TelegramUser
from summon.models import CharacterInput

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
    result = await store.catalog.upsert_character(body, actor_id=user.id)
    logger.info("catalog_changed actor=%s character=%s", user.id, character_id)
    return {"data": result}
