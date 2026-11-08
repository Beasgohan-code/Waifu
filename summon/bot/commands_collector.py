"""Collector commands and private progression views."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any, cast

from pymongo.errors import PyMongoError
from wzgram import Client, filters
from wzgram.errors import BadRequest, RPCError
from wzgram.types import CallbackQuery, Message

from summon.bot.keyboards import collection_mode_keyboard, search_page_keyboard, start_keyboard
from summon.bot.presentation import Card, Emoji, callback_text, edit_keyboard, edit_text_card
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
from summon.bot.telegram_helpers import _group, _user, callback_data, command_input, telegram_user
from summon.config import Settings
from summon.models import CollectionMode
from summon.rarities import rarity_label
from summon.repositories.character_numbers import CharacterNumberingUnavailable
from summon.repositories.collection import DailyCharacterAlreadyClaimed, DailyCharacterUnavailable
from summon.repositories.collection_insights import InsightsDataError
from summon.repositories.rarity_weights import InvalidSpawnWeights
from summon.repositories.users import DailyAlreadyClaimed, DailyStatusUnavailable, daily_status
from summon.services.collection import CollectionService, GuessRateLimited


def register_collector_commands(
    app: Client, store: Any, settings: Settings, collection: CollectionService, respond: Any
) -> None:
    search_signing_key = search_key(settings.session_secret.get_secret_value())

    @app.on_message(filters.incoming & filters.command(["start"]))
    async def start(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user:
            return
        await store.users.touch(user, datetime.now(UTC))
        keyboard = None
        if not _group(message):
            bot_user = getattr(app, "me", None)
            keyboard = start_keyboard(
                settings.webapp_url,
                getattr(bot_user, "username", None),
                user.id,
                premium=settings.premium_emoji_enabled,
            )
        await respond(
            message,
            Card(
                "Welcome to SUMMON",
                (
                    "Collect. Discover. Summon.\n\nCharacters appear as your group chats. "
                    "Guess their names, grow your collection, and trade with friends."
                ),
                emoji=Emoji.SUMMON_BRAND,
                fields=(("Get started", "/help"), ("For group admins", "/manage")),
                field_icons=(Emoji.QUESTION, Emoji.ADMIN_SHIELD),
            ),
            markup=keyboard,
        )

    @app.on_callback_query(filters.regex("^start-guide:") | filters.regex(b"^start-guide:"))
    async def start_guide(_client: Client, callback: CallbackQuery) -> None:
        actor = telegram_user(callback.from_user)
        parts = callback_data(callback).split(":")
        chat = getattr(callback.message, "chat", None)
        kind: Any = getattr(chat, "type", None)
        if (
            actor is None
            or len(parts) != 2
            or parts[1] != str(actor.id)
            or chat is None
            or chat.id != actor.id
            or getattr(kind, "value", kind) != "private"
        ):
            await callback.answer(
                callback_text("This menu belongs to another collector.", emoji=Emoji.WARNING),
                show_alert=True,
            )
            return
        try:
            allowed = await store.limiter.allow(
                f"user:{actor.id}:start-guide", limit=20, seconds=60
            )
        except (PyMongoError, OSError, TimeoutError):
            allowed = False
        if not allowed:
            await callback.answer(
                callback_text("Try again shortly.", emoji=Emoji.HOURGLASS), show_alert=True
            )
            return
        await callback.answer(callback_text("Here is your guide.", emoji=Emoji.QUESTION))
        await respond(
            callback.message,
            Card(
                "Your first summon",
                "Add SUMMON to a group and ask an administrator to enable group spawns. "
                "When a character appears, guess their name with /summon. "
                "Collect a free character with /hclaim and explore your collection "
                "with /collection.",
                emoji=Emoji.SUMMON_BRAND,
                fields=(("Full guide", "/help"), ("Group setup", "/manage")),
                field_icons=(Emoji.QUESTION, Emoji.ADMIN_SHIELD),
            ),
        )

    @app.on_message(filters.incoming & filters.command(["help", "commands", "menu"]))
    async def help_command(_client: Client, message: Message) -> None:
        user = _user(message)
        operator_fields = (
            (
                ("Catalog operator", "/upload · /addchar · /updatechar (private chat)"),
                (
                    "Media upload",
                    'Reply with /upload "Name" "Anime" 1 or /updatechar exact-id media; '
                    "choose a host, review, then publish or cancel.",
                ),
            )
            if user and user.id in settings.operator_ids and not _group(message)
            else ()
        )
        await respond(
            message,
            Card(
                "Your SUMMON guide",
                "A little universe for collectors and their communities.",
                emoji=Emoji.QUESTION,
                fields=(
                    (
                        "Collect",
                        "/summon name · /collection · /hmode · /search name (/sips) · /check id",
                    ),
                    ("Favorites", "/favorites · /favorite id on|off · /fav id"),
                    (
                        "Progress",
                        "/profile · /balance · /daily · /hclaim · /streak · /achievements · /top",
                    ),
                    ("Trade & gift", "Reply with /trade your-id their-id or /gift your-id"),
                    (
                        "Group",
                        "/manage · /spawns · /spawn · /checkspawn · /groupstats · /rules · /notes",
                    ),
                    ("Moderation", "/warn · /warnings · /unwarn · /ban"),
                    ("Other actions", "Use Telegram to mute, unmute, unban or kick."),
                )
                + operator_fields,
            ),
        )

    @app.on_message(filters.incoming & filters.command(["summon", "guess", "collect", "grab"]))
    async def guess(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user or not _group(message):
            await respond(
                message,
                Card(
                    "Find a summon",
                    "Use this command in a group when a character appears.",
                    emoji=Emoji.SEARCH,
                ),
            )
            return
        if not command.args:
            await respond(
                message,
                Card(
                    "Make a guess",
                    "Use /summon followed by the character's name.",
                    emoji=Emoji.SEARCH,
                ),
            )
            return
        try:
            character = await collection.guess(
                message.chat.id, user.model_dump(), command.args, datetime.now(UTC)
            )
        except GuessRateLimited:
            await respond(
                message,
                Card(
                    "Take a breath",
                    "Wait a few seconds before guessing again.",
                    emoji=Emoji.WARNING,
                ),
                private=True,
            )
            return
        if not character:
            await respond(
                message,
                Card(
                    "Not quite",
                    "That name doesn't match an active summon, or it has already ended.",
                ),
                private=True,
            )
            return
        await respond(
            message,
            Card(
                "A new companion!",
                f"{user.first_name} collected {character['name']}.",
                emoji=Emoji.STAR,
                fields=(
                    ("Series", character["category"]),
                    ("Rarity", rarity_label(character["rarity"])),
                    ("Reward", "+40 points · +25 XP"),
                ),
                field_icons=(Emoji.SERIES_TAG, Emoji.RARITY_STAR, Emoji.REWARD_COIN),
            ),
        )

    @app.on_message(filters.incoming & filters.command(["collection", "inventory", "harem", "inv"]))
    async def show_collection(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user:
            return
        if not await collector_rate_allowed(message, user.id, "collection"):
            return
        try:
            mode = await store.users.collection_mode(user.id)
            options: dict[str, Any] = {"limit": 12}
            if mode == "copies":
                options["sort"] = "copies"
            elif mode == "favorites":
                options["favorite"] = True
            inventory = await store.catalog.inventory(user.id, **options)
        except (PyMongoError, OSError, TimeoutError):
            await collector_read_error(message)
            return
        fields = tuple(
            (item["name"], f"{rarity_label(item['rarity'])} · ×{item['quantity']}")
            for item in inventory["items"]
        )
        empty = (
            "You have no favorites yet. Use /favorite character-id on."
            if mode == "favorites"
            else "Your collection is waiting for its first character. Guess a summon to begin."
        )
        await respond(
            message,
            Card(
                "Your collection",
                f"{inventory['total']} unique characters · {mode} mode." if fields else empty,
                emoji=Emoji.DIAMOND,
                fields=fields,
            ),
            private=True,
        )

    async def collector_rate_allowed(message: Message, actor_id: int, action: str) -> bool:
        try:
            allowed = await store.limiter.allow(
                f"user:{actor_id}:collector:{action}", limit=30, seconds=60
            )
        except (PyMongoError, OSError, TimeoutError):
            await respond(
                message,
                Card("Collector tools unavailable", "Please try again later."),
                private=True,
            )
            return False
        if not allowed:
            await respond(
                message,
                Card("Please slow down", "Too many requests; try again shortly."),
                private=True,
            )
        return bool(allowed)

    async def collector_read_error(message: Message) -> None:
        await respond(
            message,
            Card("Collector tools unavailable", "Could not read your results; try again later."),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["hmode"]))
    async def collection_display_mode(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user:
            return
        value = (command_input(message).args or "").strip().casefold()
        value = {"fav": "favorites", "favorite": "favorites"}.get(value, value)
        if value and value not in {"recent", "copies", "favorites"}:
            await respond(
                message,
                Card(
                    "Collection display",
                    "Use /hmode recent, /hmode copies or /hmode favorites.",
                    emoji=Emoji.GEAR,
                ),
                private=True,
            )
            return
        if not await collector_rate_allowed(message, user.id, "collection-mode"):
            return
        try:
            if value:
                mode = cast(CollectionMode, value)
                await store.users.set_collection_mode(user, mode, datetime.now(UTC))
            else:
                mode = await store.users.collection_mode(user.id)
        except (PyMongoError, OSError, TimeoutError):
            # A failed write may have committed; don't replay it automatically.
            await respond(
                message,
                Card(
                    "Display mode unavailable",
                    "Check /hmode before trying again.",
                    emoji=Emoji.WARNING,
                ),
                private=True,
            )
            return
        await respond(
            message,
            Card(
                "Collection display",
                "Choose how /collection shows your characters: recent, copies or favorites.",
                emoji=Emoji.GEAR,
                fields=(("Current mode", mode),),
            ),
            markup=(
                collection_mode_keyboard(user.id, mode, premium=settings.premium_emoji_enabled)
                if not _group(message)
                else None
            ),
            private=True,
        )

    @app.on_callback_query(filters.regex("^hmode:") | filters.regex(b"^hmode:"))
    async def collection_mode_callback(_client: Client, callback: CallbackQuery) -> None:
        actor = telegram_user(callback.from_user)
        parts = callback_data(callback).split(":")
        if (
            actor is None
            or len(parts) != 3
            or not parts[1].isdigit()
            or len(parts[1]) > 16
            or int(parts[1]) != actor.id
            or parts[2] not in {"recent", "copies", "favorites"}
        ):
            await callback.answer(
                callback_text(
                    "This menu belongs to another collector or has expired.", emoji=Emoji.WARNING
                ),
                show_alert=True,
            )
            return
        try:
            if not await store.limiter.allow(
                f"user:{actor.id}:collector:collection-mode", limit=30, seconds=60
            ):
                await callback.answer(
                    callback_text("Too many requests; try again shortly.", emoji=Emoji.HOURGLASS),
                    show_alert=True,
                )
                return
            mode = cast(CollectionMode, parts[2])
            await store.users.set_collection_mode(actor, mode, datetime.now(UTC))
        except (PyMongoError, OSError, TimeoutError):
            await callback.answer(
                callback_text("Check /hmode before trying again.", emoji=Emoji.WARNING),
                show_alert=True,
            )
            return
        await callback.answer(callback_text(f"Collection mode: {mode}", emoji=Emoji.DONE))
        if callback.message and hasattr(callback.message, "edit_reply_markup"):
            try:
                await edit_keyboard(
                    callback.message,
                    collection_mode_keyboard(
                        actor.id, mode, premium=settings.premium_emoji_enabled
                    ),
                    premium=settings.premium_emoji_enabled,
                )
            except (BadRequest, RPCError, OSError, TimeoutError):
                # The preference is already saved; never replay the mutation.
                pass

    @app.on_message(filters.incoming & filters.command(["ping"]))
    async def ping(_client: Client, message: Message) -> None:
        await respond(message, Card("Pong", "SUMMON is responding.", emoji=Emoji.SPARK))

    @app.on_message(filters.incoming & filters.command(["pinfo"]))
    async def premium_info(_client: Client, message: Message) -> None:
        if not _user(message) or message.from_user is None:
            return
        status = (
            "Telegram indicates that your account has Premium."
            if message.from_user.is_premium
            else "Telegram did not indicate a Premium subscription in this update."
        )
        await respond(message, Card("Telegram Premium", status, emoji=Emoji.STAR), private=True)

    @app.on_message(filters.incoming & filters.command(["check", "info"]))
    async def character_info(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user:
            return
        character_id = (command.args or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", character_id):
            await respond(
                message,
                Card("Character information", "Use /check followed by a character ID."),
                private=True,
            )
            return
        if not await collector_rate_allowed(message, user.id, "character_info"):
            return
        try:
            character = await store.catalog.details(user.id, character_id)
        except CharacterNumberingUnavailable:
            await respond(
                message,
                Card(
                    "Numbering needs review",
                    "An operator must review conflicting or unusually large legacy catalogs. "
                    "Existing character IDs and ownership have not been changed.",
                    emoji=Emoji.WARNING,
                ),
                private=True,
            )
            return
        except PyMongoError:
            await collector_read_error(message)
            return
        if not character:
            await respond(
                message, Card("Not found", "That character is not in the catalog."), private=True
            )
            return
        display_id = str(character.get("number", character["id"]))
        if character.get("number") is not None and display_id != character["id"]:
            display_id += f" (catalog key: {character['id']})"
        await respond(
            message,
            Card(
                character["name"],
                character.get("description", ""),
                emoji=Emoji.SEARCH,
                fields=(
                    ("ID", display_id),
                    ("Series", character["category"]),
                    ("Rarity", rarity_label(character["rarity"])),
                    ("Your copies", str(character.get("quantity", 0))),
                ),
                field_icons=(None, Emoji.SERIES_TAG, Emoji.RARITY_STAR, Emoji.OPEN_COLLECTION),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["stats", "server"]))
    async def community_stats(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user or not await collector_rate_allowed(message, user.id, "community_stats"):
            return
        try:
            if not await store.limiter.allow("bot:community-stats", limit=30, seconds=60):
                await respond(message, Card("Please slow down", "Try again shortly."), private=True)
                return
            counts = await store.users.community_counts()
        except PyMongoError:
            await collector_read_error(message)
            return
        await respond(
            message,
            Card(
                "SUMMON community",
                "Aggregate database counts; no private player details are shown.",
                emoji=Emoji.STATS,
                fields=(
                    ("Collectors", str(counts["collectors"])),
                    ("Groups", str(counts["groups"])),
                    ("Active characters", str(counts["characters"])),
                ),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["chancelist", "clist", "claimlist"]))
    async def rarity_weights(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user or not await collector_rate_allowed(message, user.id, "rarity_weights"):
            return
        try:
            weights = await store.catalog.spawn_weights()
        except (PyMongoError, InvalidSpawnWeights, OSError, TimeoutError):
            await collector_read_error(message)
            return
        await respond(
            message,
            Card(
                "Summon rarity weights",
                "These weights apply to group summons and free /hclaim characters. "
                "Only active, nonempty buckets participate, so the resulting odds change "
                "when a rarity has no active characters. "
                "Legacy Epic shares the Special Edition bucket.",
                emoji=Emoji.RARITY_STAR,
                fields=tuple((rarity_label(name), str(weight)) for name, weight in weights.items()),
            ),
            private=True,
        )

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

    @app.on_message(filters.incoming & filters.command(["favorites"]))
    async def show_favorites(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user or not await collector_rate_allowed(message, user.id, "favorites"):
            return
        try:
            page = await store.catalog.inventory(user.id, favorite=True, limit=10)
        except PyMongoError:
            await collector_read_error(message)
            return
        fields = tuple(
            (item["name"], f"{item['id']} · ×{item['quantity']}") for item in page["items"]
        )
        body = (
            f"Showing {len(fields)} of {page['total']} favorites. Open the Mini App for more."
            if fields
            else "You have no favorites yet. Use /favorite character-id on."
        )
        await respond(
            message, Card("Your favorites", body, emoji=Emoji.HEART, fields=fields), private=True
        )

    @app.on_message(filters.incoming & filters.command(["favorite", "fav"]))
    async def update_favorite(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user:
            return
        parts = (command.args or "").split()
        # The legacy shorthand adds one owned favorite; /favorite keeps its
        # existing explicit on/off contract. Never alter another user's data.
        if getattr(command, "command", "") == "fav" and len(parts) == 1:
            parts.append("on")
        if (
            len(parts) != 2
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", parts[0])
            or parts[1] not in {"on", "off"}
        ):
            await respond(
                message,
                Card("Update a favorite", "Use /favorite character-id on or off."),
                private=True,
            )
            return
        if not await collector_rate_allowed(message, user.id, "favorite"):
            return
        try:
            changed = await store.catalog.set_favorite(user.id, parts[0], parts[1] == "on")
        except PyMongoError:
            await respond(
                message,
                Card(
                    "Result uncertain",
                    "Could not confirm the change. Check /favorites before retrying.",
                ),
                private=True,
            )
            return
        if not changed:
            await respond(
                message,
                Card("Not in your collection", "Only owned characters can be favorited."),
                private=True,
            )
            return
        text = "Added to favorites." if parts[1] == "on" else "Removed from favorites."
        await respond(message, Card("Favorite updated", text, emoji=Emoji.HEART), private=True)

    @app.on_message(
        filters.incoming & filters.command(["achievements", "progress", "ach", "badges"])
    )
    async def show_achievements(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user or not await collector_rate_allowed(message, user.id, "achievements"):
            return
        try:
            progress = await store.users.progression(user.id, datetime.now(UTC))
        except (PyMongoError, DailyStatusUnavailable):
            await collector_read_error(message)
            return
        fields = tuple(
            (
                item["title"],
                f"{item['current']} / {item['target']} · "
                f"{'Complete' if item['completed'] else 'In progress'}",
            )
            for item in progress["achievements"]
        ) + tuple(
            (item["title"], "Complete" if item["completed"] else "Not complete")
            for item in progress["daily"]
        )
        body = "Your milestones and daily goals. Use /profile to see your level."
        await respond(
            message, Card("Your achievements", body, emoji=Emoji.STATS, fields=fields), private=True
        )

    @app.on_message(filters.incoming & filters.command(["profile", "me", "hstats"]))
    async def profile(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user:
            return
        if command.args:
            await respond(
                message,
                Card("Your collector profile", "This command shows only your own profile."),
                private=True,
            )
            return
        if not await collector_rate_allowed(message, user.id, "profile"):
            return
        try:
            await store.users.touch(user, datetime.now(UTC))
            profile_data = await store.users.profile(user.id)
            insights = await store.users.collection_insights(user.id)
        except (PyMongoError, InsightsDataError, DailyStatusUnavailable, TimeoutError):
            await collector_read_error(message)
            return
        goal = next((item for item in insights["goals"] if not item["completed"]), None)
        rarity_lines = [
            f"{rarity_label(item['rarity'])}: {item['unique']} unique · {item['copies']} copies"
            for item in insights["rarity_breakdown"]
        ]
        await respond(
            message,
            Card(
                "Collector profile",
                user.first_name + " · Collection goals reflect what you own now.",
                emoji=Emoji.STATS,
                fields=(
                    ("Level", str(profile_data["level"])),
                    ("Points", str(profile_data["points"])),
                    (
                        "Active catalogue",
                        f"{insights['active_owned']} / {insights['catalog_size']}",
                    ),
                    ("Owned copies", str(insights["total_copies"])),
                    ("Duplicate copies", str(insights["duplicate_copies"])),
                    ("Favorites", str(insights["favorite_characters"])),
                    ("Group catches", str(profile_data["catch_count"])),
                    ("Best recorded streak", f"{profile_data['best_streak']} days"),
                    (
                        "Next goal",
                        f"{goal['title']} · {goal['current']}/{goal['target']}"
                        if goal
                        else "Current collection goals complete",
                    ),
                ),
                details=("Rarity distribution", "\n".join(rarity_lines) or "No characters yet."),
                field_icons=(
                    Emoji.RARITY_STAR,
                    Emoji.REWARD_COIN,
                    Emoji.OPEN_COLLECTION,
                    Emoji.OPEN_COLLECTION,
                    Emoji.OPEN_COLLECTION,
                    Emoji.HEART,
                    Emoji.SPARK,
                    Emoji.HOURGLASS,
                    Emoji.STAR,
                ),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["balance", "bal"]))
    async def balance(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user or not await collector_rate_allowed(message, user.id, "balance"):
            return
        try:
            data = await store.users.profile(user.id)
        except (PyMongoError, DailyStatusUnavailable):
            await collector_read_error(message)
            return
        await respond(
            message,
            Card(
                "Collector balance",
                "Points are in-game progress, not money or a cash balance.",
                emoji=Emoji.REWARD_COIN,
                fields=(
                    ("Points", str(data["points"])),
                    ("Characters", str(data["unique_characters"])),
                ),
                field_icons=(Emoji.REWARD_COIN, Emoji.OPEN_COLLECTION),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["streak"]))
    async def streak(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user or not await collector_rate_allowed(message, user.id, "streak"):
            return
        try:
            data = await store.users.profile(user.id)
        except (PyMongoError, DailyStatusUnavailable):
            await collector_read_error(message)
            return
        status = daily_status(data.get("last_daily"), datetime.now(UTC))["state"]
        daily_message = {
            "available": "Your daily reward is available with /daily.",
            "claimed": "Today's daily reward has already been claimed.",
            "unavailable": "Daily status needs review; please contact support.",
        }[status]
        await respond(
            message,
            Card(
                "Daily streak",
                f"{daily_message} The streak is recalculated on your next claim, using UTC.",
                emoji=Emoji.SPARK,
                fields=(
                    ("Last recorded streak", f"{data.get('streak', 0)} days"),
                    (
                        "Best recorded streak",
                        f"{data.get('best_streak', data.get('streak', 0))} days",
                    ),
                ),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["hclaim", "claim"]))
    async def daily_character(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user:
            return
        if (command_input(message).args or "").strip():
            await respond(
                message,
                Card("Free daily character", "Use /hclaim without arguments."),
                private=True,
            )
            return
        if not await collector_rate_allowed(message, user.id, "daily_character"):
            return
        try:
            allowed = await store.limiter.allow("bot:daily-character", limit=100, seconds=60)
        except (PyMongoError, OSError, TimeoutError):
            await collector_read_error(message)
            return
        if not allowed:
            await respond(message, Card("Please slow down", "Try again shortly."), private=True)
            return
        if app.me is None or not isinstance(message.id, int) or message.id <= 0:
            await collector_read_error(message)
            return
        material = f"{app.me.id}:{message.chat.id}:{message.id}".encode("ascii")
        message_id = "daily-character-message:" + hashlib.sha256(material).hexdigest()
        try:
            character = await collection.claim_daily_character(
                user, datetime.now(UTC), message_id=message_id
            )
        except DailyCharacterAlreadyClaimed:
            await respond(
                message,
                Card(
                    "Daily character already claimed",
                    "You can claim another after 00:00 UTC.",
                    emoji=Emoji.HOURGLASS,
                ),
                private=True,
            )
            return
        except (DailyCharacterUnavailable, InvalidSpawnWeights):
            await respond(
                message,
                Card(
                    "No character is available",
                    "There must be an active character in a nonzero rarity bucket. Try later.",
                    emoji=Emoji.WARNING,
                ),
                private=True,
            )
            return
        except (PyMongoError, OSError, TimeoutError):
            await respond(
                message,
                Card(
                    "Claim unconfirmed",
                    "Check /collection before trying again; a claim may have succeeded.",
                    emoji=Emoji.WARNING,
                ),
                private=True,
            )
            return
        await respond(
            message,
            Card(
                "Daily character claimed",
                "Added to your collection for free. One character is available per UTC day.",
                emoji=Emoji.GIFT_ACTION,
                fields=(
                    ("Name", character["name"]),
                    ("ID", character["id"]),
                    ("Series", character["category"]),
                    ("Rarity", rarity_label(character["rarity"])),
                ),
                field_icons=(None, None, Emoji.SERIES_TAG, Emoji.RARITY_STAR),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["daily"]))
    async def daily(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user:
            return
        try:
            reward = await store.users.daily(user, datetime.now(UTC))
        except DailyAlreadyClaimed:
            await respond(
                message,
                Card(
                    "All caught up",
                    "Today's reward has already been claimed. Come back after 00:00 UTC.",
                    emoji=Emoji.HOURGLASS,
                ),
                private=True,
            )
            return
        except DailyStatusUnavailable:
            await respond(
                message,
                Card(
                    "Daily unavailable",
                    "Your daily status needs review. Please contact support.",
                    emoji=Emoji.WARNING,
                ),
                private=True,
            )
            return
        await respond(
            message,
            Card(
                "Daily reward",
                "A little something for your journey.",
                emoji=Emoji.COIN,
                fields=(
                    ("Points", f"+{reward['points']}"),
                    ("Experience", "+20 XP"),
                    ("Streak", f"{reward['streak']} days"),
                ),
                field_icons=(Emoji.REWARD_COIN, Emoji.RARITY_STAR, Emoji.SPARK),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["top", "leaderboard", "rank"]))
    async def leaderboard(_client: Client, message: Message) -> None:
        rows = await store.users.leaderboard(limit=10)
        fields = tuple(
            (f"{index}. {row['name']}", f"{row['catch_count']} summons")
            for index, row in enumerate(rows, 1)
        )
        await respond(
            message,
            Card(
                "Collectors leaderboard",
                "The community's most active collectors."
                if fields
                else "The leaderboard is waiting for its first collector.",
                emoji=Emoji.CROWN,
                fields=fields,
            ),
        )
