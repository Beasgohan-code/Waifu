"""API routes for a single SUMMON domain."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request

from summon.api.dependencies import get_current_user, get_store, limit_action
from summon.api.schemas import (
    GiftRequest,
    TradeID,
    TradeRequest,
)
from summon.auth import TelegramUser
from summon.services.gifting import GiftService
from summon.services.trading import TradingService

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


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
