"""Stable API router facade; domain route modules keep transport concerns separate."""

from __future__ import annotations

from fastapi import APIRouter

from summon.api import routes_admin, routes_catalog, routes_exchange, routes_groups
from summon.api.dependencies import get_bot, permissions
from summon.api.routes_admin import save_character
from summon.api.routes_catalog import (
    character_detail,
    characters,
    daily,
    favorite,
    inventory,
    inventory_pages,
    leaderboard,
    progression,
    wishlist,
)
from summon.api.routes_exchange import (
    accept_gift,
    accept_trade,
    cancel_gift,
    cancel_trade,
    gifts,
    propose_gift,
    propose_trade,
    trades,
)
from summon.api.routes_groups import (
    delete_group_auto_reply,
    delete_group_note,
    group_detail,
    groups,
    list_group_auto_replies,
    list_group_notes,
    moderate,
    review_warnings,
    revoke_warning,
    save_group_auto_reply,
    save_group_note,
    update_group,
)
from summon.api.schemas import (
    AutoReplyWrite,
    CharacterID,
    GiftRequest,
    ModerationRequest,
    NoteDelete,
    NoteName,
    NoteWrite,
    Toggle,
    TradeID,
    TradeRequest,
    WarningGroupID,
    WarningID,
    WarningRevocation,
    WarningTargetID,
)

__all__ = [
    "router",
    "get_bot",
    "permissions",
    "CharacterID",
    "TradeID",
    "WarningID",
    "WarningGroupID",
    "WarningTargetID",
    "NoteName",
    "Toggle",
    "TradeRequest",
    "GiftRequest",
    "ModerationRequest",
    "WarningRevocation",
    "NoteWrite",
    "NoteDelete",
    "AutoReplyWrite",
    "characters",
    "character_detail",
    "inventory",
    "inventory_pages",
    "favorite",
    "wishlist",
    "daily",
    "progression",
    "leaderboard",
    "trades",
    "propose_trade",
    "accept_trade",
    "cancel_trade",
    "gifts",
    "propose_gift",
    "accept_gift",
    "cancel_gift",
    "groups",
    "group_detail",
    "update_group",
    "moderate",
    "list_group_auto_replies",
    "save_group_auto_reply",
    "delete_group_auto_reply",
    "list_group_notes",
    "save_group_note",
    "delete_group_note",
    "review_warnings",
    "revoke_warning",
    "save_character",
]

router = APIRouter()
for domain_router in (
    routes_catalog.router,
    routes_exchange.router,
    routes_groups.router,
    routes_admin.router,
):
    router.include_router(domain_router)
