"""Native Wzgram search feature handlers."""

from __future__ import annotations

from typing import Any

from pymongo.errors import PyMongoError
from wzgram import Client, filters
from wzgram.errors import RPCError
from wzgram.types import CallbackQuery, Message

from summon.bot.keyboards import search_page_keyboard
from summon.bot.modules.context import CollectorContext
from summon.bot.presentation import Card, Emoji, callback_text, edit_text_card
from summon.bot.search_paging import (
    SEARCH_MAX_PAGES,
    SEARCH_PAGE_SIZE,
    parse_search_callback,
    search_callback,
    search_card,
    search_key,
    search_term,
    source_term,
    valid_search_callback,
)
from summon.bot.telegram_helpers import _user, callback_data, command_input, telegram_user


def register(context: CollectorContext) -> None:
    app = context.app
    store = context.store
    settings = context.settings
    respond = context.respond
    collector_rate_allowed = context.rate_allowed
    collector_read_error = context.read_error
    search_signing_key = search_key(settings.session_secret.get_secret_value())

    def search_markup(
        source: Message, actor_id: int, bot_id: int, page: int, total: int
    ) -> Any | None:
        previous = (
            search_callback(search_signing_key, bot_id, actor_id, source, page - 1)
            if page > 1
            else None
        )
        following = (
            search_callback(search_signing_key, bot_id, actor_id, source, page + 1)
            if page < SEARCH_MAX_PAGES and page * SEARCH_PAGE_SIZE < total
            else None
        )
        if not previous and not following:
            return None
        return search_page_keyboard(previous, following, premium=settings.premium_emoji_enabled)

    @app.on_message(filters.incoming & filters.command(["search", "find", "sips"]))
    async def search_characters(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user:
            return
        term = search_term(command.args)
        if term is None:
            await respond(
                message,
                Card("Search characters", "Use /search followed by 1–80 characters of a name."),
                private=True,
            )
            return
        if not await collector_rate_allowed(message, user.id, "search"):
            return
        try:
            result = await store.catalog.list(user.id, search=term, limit=SEARCH_PAGE_SIZE)
        except (PyMongoError, OSError, TimeoutError):
            await collector_read_error(message)
            return
        identity = getattr(app, "me", None)
        can_navigate = bool(
            identity
            and isinstance(identity.id, int)
            and not isinstance(identity.id, bool)
            and 0 < identity.id < 2**53
            and source_term(message, user.id, identity.username) == term
        )
        markup = (
            search_markup(message, user.id, identity.id, 1, result["total"])
            if can_navigate and identity
            else None
        )
        await respond(
            message,
            search_card(result, 1, can_navigate=can_navigate),
            markup=markup,
            private=True,
            standard=True,
        )

    @app.on_callback_query(filters.regex(r"^catalog-search:") | filters.regex(rb"^catalog-search:"))
    async def search_page(_client: Client, callback: CallbackQuery) -> None:
        actor = telegram_user(callback.from_user)
        data = callback_data(callback)
        parsed = parse_search_callback(data)
        identity = getattr(app, "me", None)
        chat = getattr(callback.message, "chat", None)
        kind: Any = getattr(chat, "type", None)
        if (
            actor is None
            or parsed is None
            or identity is None
            or not isinstance(identity.id, int)
            or isinstance(identity.id, bool)
            or not 0 < identity.id < 2**53
            or chat is None
            or chat.id != actor.id
            or getattr(kind, "value", kind) != "private"
        ):
            await callback.answer(
                callback_text("This search is unavailable.", emoji=Emoji.WARNING), show_alert=True
            )
            return
        message_id, page, _token = parsed
        try:
            if not await store.limiter.allow(
                f"user:{actor.id}:collector:search", limit=30, seconds=60
            ):
                await callback.answer(
                    callback_text("Try again shortly.", emoji=Emoji.HOURGLASS), show_alert=True
                )
                return
            source = await app.get_messages(actor.id, message_ids=message_id, replies=0)
            if not isinstance(source, Message) or source.empty or source.id != message_id:
                raise ValueError("Original search is unavailable")
            term = source_term(source, actor.id, identity.username)
            if term is None:
                raise ValueError("Original search changed or expired")
            expected = search_callback(search_signing_key, identity.id, actor.id, source, page)
            if not valid_search_callback(expected, data):
                raise ValueError("Original search changed")
            result = await store.catalog.list(
                actor.id, search=term, page=page, limit=SEARCH_PAGE_SIZE
            )
        except ValueError:
            await callback.answer(
                callback_text(
                    "This search expired or changed. Send /search again.", emoji=Emoji.WARNING
                ),
                show_alert=True,
            )
            return
        except (PyMongoError, RPCError, OSError, TimeoutError):
            await callback.answer(
                callback_text("Search is unavailable. Try again later.", emoji=Emoji.WARNING),
                show_alert=True,
            )
            return
        markup = search_markup(source, actor.id, identity.id, page, result["total"])
        try:
            await edit_text_card(
                callback.message,
                search_card(result, page, can_navigate=True),
                premium=settings.premium_emoji_enabled,
                reply_markup=markup,
            )
        except (RPCError, OSError, TimeoutError):
            # Do not repeat an edit whose outcome may be unknown.
            await callback.answer(
                callback_text(
                    "Could not update the page. Send /search again.", emoji=Emoji.WARNING
                ),
                show_alert=True,
            )
            return
        await callback.answer(callback_text(f"Page {page}", emoji=Emoji.SEARCH))
