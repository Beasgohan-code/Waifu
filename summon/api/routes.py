"""HTTP transport: validation and authorization live here; rules live in services."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo.errors import PyMongoError

from summon.api.dependencies import get_current_user, get_store, limit_action
from summon.auth import TelegramUser
from summon.group_notes import (
    MAX_NOTE_LENGTH,
    NOTE_NAME_PATTERN,
    NOTE_REVISION_PATTERN,
    validate_note_content,
)
from summon.models import CharacterInput, GroupSettingsUpdate, InventorySort, Rarity
from summon.repositories.groups import public_group
from summon.services.gifting import GiftService
from summon.services.group_notes import GroupNotesService
from summon.services.moderation import ModerationService, PermissionDenied, require_group_admin
from summon.services.trading import TradingService

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)
CharacterID = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
TradeID = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")]
WarningID = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")]
WarningGroupID = Annotated[int, Path(ge=-(2**53 - 1), lt=0)]
WarningTargetID = Annotated[int, Path(gt=0, le=2**53 - 1)]
NoteName = Annotated[str, Path(min_length=1, max_length=32, pattern=NOTE_NAME_PATTERN)]


class Toggle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: bool


class TradeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receiver_id: int = Field(gt=0, le=2**53 - 1)
    offer_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    request_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")


class GiftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receiver_id: int = Field(gt=0, le=2**53 - 1)
    character_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")


class ModerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: int = Field(gt=0, le=2**53 - 1)
    action: Literal["ban", "unban", "mute", "unmute", "kick", "warn"]
    reason: str = Field(default="", max_length=500)
    duration_seconds: int | None = Field(default=None, ge=60, le=366 * 86400)


class WarningRevocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=500)


class NoteWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=MAX_NOTE_LENGTH)
    # Explicit null creates only; an existing revision updates only that version.
    expected_revision: str | None = Field(pattern=NOTE_REVISION_PATTERN)

    @field_validator("content")
    @classmethod
    def valid_content(cls, value: str) -> str:
        validate_note_content(value)
        return value


class NoteDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str = Field(pattern=NOTE_REVISION_PATTERN)


def get_bot(request: Request) -> Any:
    bot = request.app.state.telegram_bot
    if bot is None:
        raise HTTPException(503, "Group management is not connected to Telegram")
    return bot


def permissions(member: Any) -> dict[str, bool]:
    status = getattr(member, "status", "")
    status = str(getattr(status, "value", status))
    owner = status == "creator"
    return {
        "can_change_info": owner or bool(getattr(member, "can_change_info", False)),
        "can_restrict_members": owner or bool(getattr(member, "can_restrict_members", False)),
    }


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
    character = await store.catalog.details(user.id, character_id)
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


@router.get("/trades")
async def trades(
    user: TelegramUser = Depends(get_current_user), store: Any = Depends(get_store)
) -> dict[str, Any]:
    return {"data": await store.trades.list(user.id)}


@router.post("/trades", status_code=201)
async def propose_trade(
    body: TradeRequest,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    await limit_action(request, user, "trade", limit=10)
    trade = await TradingService(store.trades).propose(
        user.id, body.receiver_id, body.offer_id, body.request_id, datetime.now(UTC)
    )
    return {"data": trade}


@router.post("/trades/{trade_id}/accept")
async def accept_trade(
    trade_id: TradeID,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    await limit_action(request, user, "trade", limit=10)
    return {
        "data": await TradingService(store.trades).confirm(trade_id, user.id, datetime.now(UTC))
    }


@router.post("/trades/{trade_id}/cancel")
async def cancel_trade(
    trade_id: TradeID,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    await limit_action(request, user, "trade", limit=10)
    return {"data": await store.trades.cancel(trade_id, user.id, datetime.now(UTC))}


@router.get("/gifts")
async def gifts(
    user: TelegramUser = Depends(get_current_user), store: Any = Depends(get_store)
) -> dict[str, Any]:
    return {"data": await store.gifts.list(user.id)}


@router.post("/gifts", status_code=201)
async def propose_gift(
    body: GiftRequest,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    await limit_action(request, user, "gift", limit=10)
    gift = await GiftService(store.gifts).propose(
        user.id, body.receiver_id, body.character_id, datetime.now(UTC)
    )
    return {"data": gift}


@router.post("/gifts/{gift_id}/accept")
async def accept_gift(
    gift_id: TradeID,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    await limit_action(request, user, "gift", limit=10)
    return {"data": await GiftService(store.gifts).confirm(gift_id, user.id, datetime.now(UTC))}


@router.post("/gifts/{gift_id}/cancel")
async def cancel_gift(
    gift_id: TradeID,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    await limit_action(request, user, "gift", limit=10)
    return {"data": await store.gifts.cancel(gift_id, user.id, datetime.now(UTC))}


@router.get("/groups")
async def groups(
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    if request.app.state.settings.preview_mode:
        return {"data": await store.groups.preview_list()}
    bot = get_bot(request)
    result = []
    for group_id in await store.groups.linked_ids(user.id):
        try:
            member = await require_group_admin(bot, group_id, user.id)
        except (PermissionDenied, TelegramBadRequest):
            logger.info("A previously linked group is no longer manageable")
            continue
        doc = await store.groups.get(group_id)
        if doc:
            group = public_group(doc)
            group["permissions"] = permissions(member)
            result.append(group)
    return {"data": result}


@router.get("/groups/{group_id}")
async def group_detail(
    group_id: int,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    if request.app.state.settings.preview_mode:
        group = await store.groups.preview_get(group_id)
        if not group:
            raise HTTPException(404, "Group not found")
        return {"data": group}
    member = await require_group_admin(get_bot(request), group_id, user.id)
    group = await store.groups.get(group_id)
    if not group:
        raise HTTPException(404, "Group not found; use /manage in the group first")
    await store.groups.link_admin(group_id, user.id, datetime.now(UTC))
    result = public_group(group)
    result["permissions"] = permissions(member)
    activity = await store.groups.recent_activity(group_id)
    # A generic admin may manage other settings without authority to review
    # sensitive moderation reasons, targets, or warning corrections.
    if not result["permissions"]["can_restrict_members"]:
        activity = [item for item in activity if item.get("kind") != "moderation"]
    result["recent_activity"] = activity
    return {"data": result}


@router.patch("/groups/{group_id}/settings")
async def update_group(
    group_id: int,
    body: GroupSettingsUpdate,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    await limit_action(request, user, "group_settings", limit=20)
    patch = body.model_dump(exclude_unset=True, exclude_none=True)
    if not patch:
        raise HTTPException(400, "Choose at least one setting")
    protection = {
        "link_filter",
        "flood_enabled",
        "flood_limit",
        "flood_window_seconds",
        "duplicate_enabled",
        "duplicate_limit",
        "mention_limit",
        "blocked_keywords",
        "warning_limit",
    }
    bot = get_bot(request)
    if set(patch) & protection:
        await require_group_admin(bot, group_id, user.id, "can_restrict_members")
    if set(patch) - protection:
        await require_group_admin(bot, group_id, user.id, "can_change_info")
    group = await store.groups.update_settings(group_id, user.id, patch, datetime.now(UTC))
    return {"data": group}


@router.post("/groups/{group_id}/moderation")
async def moderate(
    group_id: int,
    body: ModerationRequest,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    await limit_action(request, user, "moderation", limit=20)
    result = await ModerationService(get_bot(request), store.groups).act(
        group_id,
        user.id,
        body.target_id,
        body.action,
        body.reason,
        datetime.now(UTC),
        body.duration_seconds,
    )
    return {"data": result}


@router.get("/groups/{group_id}/notes")
async def list_group_notes(
    group_id: WarningGroupID,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    if request.app.state.settings.preview_mode:
        return {"data": await store.groups.preview_notes(group_id)}
    await limit_action(request, user, "notes_read", limit=30)
    try:
        result = await GroupNotesService(get_bot(request), store.notes).list_for_admin(
            group_id, user.id
        )
    except TelegramAPIError:
        raise HTTPException(503, "Could not verify current group permissions") from None
    return {"data": result}


@router.put("/groups/{group_id}/notes/{name}")
async def save_group_note(
    group_id: WarningGroupID,
    name: NoteName,
    body: NoteWrite,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    await limit_action(request, user, "notes_write", limit=20)
    try:
        result = await GroupNotesService(get_bot(request), store.notes).save(
            group_id, user.id, name, body.content, body.expected_revision, datetime.now(UTC)
        )
    except ValueError:
        raise HTTPException(422, "Invalid note input") from None
    except TelegramAPIError:
        raise HTTPException(503, "Could not verify current group permissions") from None
    except PyMongoError:
        logger.warning("Group note change could not be confirmed")
        raise HTTPException(
            503, "Could not confirm the change; review notes before retrying"
        ) from None
    return {"data": result}


@router.post("/groups/{group_id}/notes/{name}/delete")
async def delete_group_note(
    group_id: WarningGroupID,
    name: NoteName,
    body: NoteDelete,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    await limit_action(request, user, "notes_write", limit=20)
    try:
        result = await GroupNotesService(get_bot(request), store.notes).delete(
            group_id, user.id, name, body.expected_revision, datetime.now(UTC)
        )
    except ValueError:
        raise HTTPException(422, "Invalid note input") from None
    except TelegramAPIError:
        raise HTTPException(503, "Could not verify current group permissions") from None
    except PyMongoError:
        logger.warning("Group note removal could not be confirmed")
        raise HTTPException(
            503, "Could not confirm the removal; review notes before retrying"
        ) from None
    return {"data": result}


@router.get("/groups/{group_id}/members/{target_id}/warnings")
async def review_warnings(
    group_id: WarningGroupID,
    target_id: WarningTargetID,
    request: Request,
    before: str | None = Query(default=None, pattern=r"^[a-f0-9]{32}$"),
    limit: int = Query(default=10, ge=1, le=25),
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    if request.app.state.settings.preview_mode:
        return {
            "data": await store.groups.preview_warnings(
                group_id, target_id, before=before, limit=limit
            )
        }
    await limit_action(request, user, "warning_review", limit=30)
    try:
        result = await ModerationService(get_bot(request), store.groups).list_warnings(
            group_id, user.id, target_id, before=before, limit=limit
        )
    except TelegramAPIError:
        raise HTTPException(503, "Could not verify current group permissions") from None
    return {"data": result}


@router.post("/groups/{group_id}/members/{target_id}/warnings/{warning_id}/revoke")
async def revoke_warning(
    group_id: WarningGroupID,
    target_id: WarningTargetID,
    warning_id: WarningID,
    body: WarningRevocation,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    await limit_action(request, user, "warning_revoke", limit=20)
    try:
        result = await ModerationService(get_bot(request), store.groups).revoke_warning(
            group_id, user.id, target_id, warning_id, body.reason, datetime.now(UTC)
        )
    except TelegramAPIError:
        raise HTTPException(503, "Could not verify current group permissions") from None
    except PyMongoError:
        logger.warning("Warning correction could not be confirmed")
        raise HTTPException(
            503, "Could not confirm the correction; review warnings before retrying"
        ) from None
    return {"data": result}


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
