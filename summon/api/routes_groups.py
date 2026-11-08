"""API routes for a single SUMMON domain."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pymongo.errors import PyMongoError
from wzgram.errors import RPCError, UserNotParticipant

from summon.api.dependencies import get_bot, get_current_user, get_store, limit_action, permissions
from summon.api.schemas import (
    AutoReplyWrite,
    ModerationRequest,
    NoteDelete,
    NoteName,
    NoteWrite,
    WarningGroupID,
    WarningID,
    WarningRevocation,
    WarningTargetID,
)
from summon.auth import TelegramUser
from summon.models import GroupSettingsUpdate
from summon.repositories.group_insights import GroupInsightsDataError
from summon.repositories.groups import public_group
from summon.services.auto_replies import AutoReplyService
from summon.services.group_notes import GroupNotesService
from summon.services.moderation import ModerationService, PermissionDenied, require_group_admin

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


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
        except (PermissionDenied, UserNotParticipant):
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


@router.get("/groups/{group_id}/summon-insights")
async def group_summon_insights(
    group_id: WarningGroupID,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    if request.app.state.settings.preview_mode:
        if not await store.groups.preview_get(group_id):
            raise HTTPException(404, "Group not found")
    else:
        await limit_action(request, user, "group_summon_insights", limit=20)
        await require_group_admin(get_bot(request), group_id, user.id)
        if not await store.groups.get(group_id):
            raise HTTPException(404, "Group not found; use /manage in the group first")
        if not await store.limiter.allow(f"group:{group_id}:summon-insights", limit=30, seconds=60):
            raise HTTPException(429, "Too many requests; please try again shortly")
    try:
        result = await store.collection.group_summon_insights(group_id, user.id, datetime.now(UTC))
    except (GroupInsightsDataError, PyMongoError, TimeoutError):
        raise HTTPException(503, "Summon insights are temporarily unavailable") from None
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


@router.get("/groups/{group_id}/auto-replies")
async def list_group_auto_replies(
    group_id: WarningGroupID,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    if request.app.state.settings.preview_mode:
        return {"data": await store.groups.preview_auto_replies(group_id)}
    await limit_action(request, user, "auto_replies_read", limit=30)
    try:
        result = await AutoReplyService(get_bot(request), store.auto_replies).list_for_admin(
            group_id, user.id
        )
    except RPCError:
        raise HTTPException(503, "Could not verify current group permissions") from None
    return {"data": result}


@router.put("/groups/{group_id}/auto-replies/{name}")
async def save_group_auto_reply(
    group_id: WarningGroupID,
    name: NoteName,
    body: AutoReplyWrite,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    await limit_action(request, user, "auto_replies_write", limit=20)
    try:
        result = await AutoReplyService(get_bot(request), store.auto_replies).save(
            group_id,
            user.id,
            name,
            body.trigger,
            body.response,
            body.enabled,
            body.expected_revision,
            datetime.now(UTC),
        )
    except ValueError:
        raise HTTPException(422, "Invalid auto-reply input") from None
    except RPCError:
        raise HTTPException(503, "Could not verify current group permissions") from None
    except PyMongoError:
        logger.warning("Group auto-reply change could not be confirmed")
        raise HTTPException(
            503, "Could not confirm the change; review auto replies before retrying"
        ) from None
    return {"data": result}


@router.post("/groups/{group_id}/auto-replies/{name}/delete")
async def delete_group_auto_reply(
    group_id: WarningGroupID,
    name: NoteName,
    body: NoteDelete,
    request: Request,
    user: TelegramUser = Depends(get_current_user),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    await limit_action(request, user, "auto_replies_write", limit=20)
    try:
        result = await AutoReplyService(get_bot(request), store.auto_replies).delete(
            group_id, user.id, name, body.expected_revision, datetime.now(UTC)
        )
    except ValueError:
        raise HTTPException(422, "Invalid auto-reply input") from None
    except RPCError:
        raise HTTPException(503, "Could not verify current group permissions") from None
    except PyMongoError:
        logger.warning("Group auto-reply removal could not be confirmed")
        raise HTTPException(
            503, "Could not confirm the removal; review auto replies before retrying"
        ) from None
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
    except RPCError:
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
    except RPCError:
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
    except RPCError:
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
    except RPCError:
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
    except RPCError:
        raise HTTPException(503, "Could not verify current group permissions") from None
    except PyMongoError:
        logger.warning("Warning correction could not be confirmed")
        raise HTTPException(
            503, "Could not confirm the correction; review warnings before retrying"
        ) from None
    return {"data": result}
