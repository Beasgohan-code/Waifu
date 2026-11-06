"""One aiogram router owns every SUMMON update and command."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from pymongo.errors import PyMongoError

from summon.auth import TelegramUser
from summon.bot.presentation import (
    Card,
    Emoji,
    button_label,
    send_card,
    send_ephemeral_card,
    send_photo_card,
)
from summon.bot.protection import GroupProtectionMiddleware, is_group_user_content
from summon.config import Settings
from summon.group_notes import NoteConflict, NoteStorageError
from summon.models import GroupSettings
from summon.repositories.users import DailyAlreadyClaimed
from summon.services.collection import CollectionService, GuessRateLimited
from summon.services.gifting import GiftConflict, GiftForbidden, GiftNotFound, GiftService
from summon.services.group_notes import GroupNotesService
from summon.services.moderation import (
    ModerationService,
    PermissionDenied,
    require_group_admin,
)
from summon.services.trading import TradeConflict, TradeForbidden, TradeNotFound, TradingService

logger = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Open SUMMON"),
    BotCommand(command="help", description="Explore commands"),
    BotCommand(command="summon", description="Guess a character in your group"),
    BotCommand(command="collection", description="View your collection"),
    BotCommand(command="profile", description="View your collector profile"),
    BotCommand(command="daily", description="Claim your daily reward"),
    BotCommand(command="trade", description="Offer a character trade"),
    BotCommand(command="gift", description="Offer a character as a gift"),
    BotCommand(command="top", description="View the collectors leaderboard"),
    BotCommand(command="manage", description="Connect a group you administer"),
    BotCommand(command="rules", description="Read group rules"),
    BotCommand(command="notes", description="List the group's public notes"),
    BotCommand(command="note", description="Read a group note by name"),
]


def group_commands() -> list[BotCommand]:
    """Personal commands stay private in groups; trade and catch remain public."""
    personal = {"collection", "profile", "daily", "manage"}
    return [
        command.model_copy(update={"is_ephemeral": True})
        if command.command in personal
        else command.model_copy()
        for command in COMMANDS
    ]


def _user(message: Message) -> TelegramUser | None:
    if not message.from_user or message.from_user.is_bot or message.sender_chat:
        return None
    return TelegramUser(
        id=message.from_user.id,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        username=message.from_user.username,
    )


def _group(message: Message) -> bool:
    return message.chat.type in {"group", "supergroup"}


def _duration(value: str) -> int | None:
    match = re.fullmatch(r"(\d{1,5})([mhd])", value.lower())
    if not match:
        return None
    amount = int(match.group(1)) * {"m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return amount if 60 <= amount <= 366 * 86400 else None


def build_router(store: Any, settings: Settings, rich_client: Any | None = None) -> Router:
    router = Router(name="summon")
    collection = CollectionService(store.collection, store.catalog)
    trading = TradingService(store.trades)
    gifting = GiftService(store.gifts)
    router.message.outer_middleware(GroupProtectionMiddleware(store.groups))
    router.edited_message.outer_middleware(GroupProtectionMiddleware(store.groups, edited=True))

    async def respond(
        message: Message,
        card: Card,
        *,
        markup: Any | None = None,
        private: bool = False,
        standard: bool = False,
    ) -> Any:
        if private and _group(message) and message.from_user:
            try:
                return await send_ephemeral_card(
                    message.bot,
                    message.chat.id,
                    message.from_user.id,
                    card,
                    premium=settings.premium_emoji_enabled,
                    message_thread_id=message.message_thread_id,
                    incoming_ephemeral_id=message.ephemeral_message_id,
                )
            except TelegramForbiddenError:
                return await send_card(
                    message.bot,
                    message.chat.id,
                    Card("Open your private chat", "Start SUMMON privately to see this response."),
                    premium=settings.premium_emoji_enabled,
                    message_thread_id=message.message_thread_id,
                )
        return await send_card(
            message.bot,
            message.chat.id,
            card,
            rich_client=None if standard else rich_client,
            bot_api_rich=False if standard else settings.bot_api_rich_enabled,
            premium=settings.premium_emoji_enabled,
            reply_markup=markup,
            message_thread_id=message.message_thread_id,
        )

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        user = _user(message)
        if not user:
            return
        await store.users.touch(user, datetime.now(UTC))
        keyboard = None
        if not _group(message) and settings.webapp_url:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=button_label(
                                "Open the collection",
                                Emoji.SPARK,
                                premium=settings.premium_emoji_enabled,
                            ),
                            web_app=WebAppInfo(url=settings.webapp_url),
                            style="primary",
                            icon_custom_emoji_id=Emoji.SPARK.value[1]
                            if settings.premium_emoji_enabled
                            else None,
                        )
                    ]
                ]
            )
        await respond(
            message,
            Card(
                "Welcome to SUMMON",
                (
                    "Collect. Discover. Summon.\n\nCharacters appear as your group chats. "
                    "Guess their names, grow your collection, and trade with friends."
                ),
                fields=(("Get started", "/help"), ("For group admins", "/manage")),
            ),
            markup=keyboard,
        )

    @router.message(Command("help", "commands"))
    async def help_command(message: Message) -> None:
        await respond(
            message,
            Card(
                "Your SUMMON guide",
                "A little universe for collectors and their communities.",
                emoji=Emoji.QUESTION,
                fields=(
                    ("Collect", "/summon name · /collection · /profile"),
                    ("Progress", "/daily · /top"),
                    ("Trade & gift", "Reply with /trade your-id their-id or /gift your-id"),
                    ("Group", "/manage · /spawns · /rules · /notes · /note name"),
                    ("Moderation", "/warn · /warnings · /unwarn · /mute · /ban · /kick"),
                ),
            ),
        )

    @router.message(Command("summon", "guess"))
    async def guess(message: Message, command: CommandObject) -> None:
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
                    ("Rarity", character["rarity"].title()),
                    ("Reward", "+40 points · +25 XP"),
                ),
            ),
        )

    @router.message(Command("collection", "inventory"))
    async def show_collection(message: Message) -> None:
        user = _user(message)
        if not user:
            return
        inventory = await store.catalog.inventory(user.id, limit=12)
        fields = tuple(
            (item["name"], f"{item['rarity'].title()} · ×{item['quantity']}")
            for item in inventory["items"]
        )
        await respond(
            message,
            Card(
                "Your collection",
                f"{inventory['total']} unique characters."
                if fields
                else (
                    "Your collection is waiting for its first character. "
                    "Guess a summon in a group to begin."
                ),
                emoji=Emoji.DIAMOND,
                fields=fields,
            ),
            private=True,
        )

    @router.message(Command("profile"))
    async def profile(message: Message) -> None:
        user = _user(message)
        if not user:
            return
        await store.users.touch(user, datetime.now(UTC))
        profile_data = await store.users.profile(user.id)
        await respond(
            message,
            Card(
                "Collector profile",
                user.first_name,
                emoji=Emoji.STATS,
                fields=(
                    ("Level", str(profile_data["level"])),
                    ("Points", str(profile_data["points"])),
                    (
                        "Collection",
                        f"{profile_data['unique_characters']} / {profile_data['catalog_size']}",
                    ),
                    ("Total summons", str(profile_data["catch_count"])),
                ),
            ),
            private=True,
        )

    @router.message(Command("daily"))
    async def daily(message: Message) -> None:
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
            ),
            private=True,
        )

    @router.message(Command("top", "leaderboard"))
    async def leaderboard(message: Message) -> None:
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

    @router.message(Command("trade"))
    async def propose_trade(message: Message, command: CommandObject) -> None:
        user = _user(message)
        target = message.reply_to_message.from_user if message.reply_to_message else None
        args = (command.args or "").split()
        if not user or not _group(message) or not target or target.is_bot or len(args) != 2:
            await respond(
                message,
                Card(
                    "Make a trade",
                    "Reply to another player with /trade your-character-id their-character-id.",
                ),
            )
            return
        if not await store.limiter.allow(f"user:{user.id}:trade", limit=10, seconds=60):
            await respond(
                message,
                Card("Take a breath", "Too many trade actions; try again shortly."),
                private=True,
            )
            return
        try:
            trade = await trading.propose(user.id, target.id, args[0], args[1], datetime.now(UTC))
        except TradeConflict as exc:
            await respond(
                message, Card("Trade unavailable", str(exc), emoji=Emoji.WARNING), private=True
            )
            return
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=button_label(
                            "Accept", Emoji.CHECK, premium=settings.premium_emoji_enabled
                        ),
                        callback_data=f"trade:accept:{trade['id']}",
                        style="success",
                        icon_custom_emoji_id=Emoji.CHECK.value[1]
                        if settings.premium_emoji_enabled
                        else None,
                    ),
                    InlineKeyboardButton(
                        text=button_label(
                            "Cancel", Emoji.CANCEL, premium=settings.premium_emoji_enabled
                        ),
                        callback_data=f"trade:cancel:{trade['id']}",
                        style="danger",
                        icon_custom_emoji_id=Emoji.CANCEL.value[1]
                        if settings.premium_emoji_enabled
                        else None,
                    ),
                ]
            ]
        )
        await respond(
            message,
            Card(
                "Trade offer",
                (
                    f"{user.first_name} offered {args[0]} for {target.first_name}'s {args[1]}.\n"
                    "Only the recipient can accept; the offer expires in 15 minutes."
                ),
                emoji=Emoji.TRADE,
            ),
            markup=markup,
        )

    @router.callback_query(F.data.startswith("trade:"))
    async def trade_callback(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if (
            len(parts) != 3
            or parts[1] not in {"accept", "cancel"}
            or not re.fullmatch(r"[a-f0-9]{32}", parts[2])
        ):
            await callback.answer("Invalid trade", show_alert=True)
            return
        if not await store.limiter.allow(
            f"user:{callback.from_user.id}:trade", limit=10, seconds=60
        ):
            await callback.answer("Too many trade actions; try again shortly.", show_alert=True)
            return
        try:
            if parts[1] == "accept":
                await trading.confirm(parts[2], callback.from_user.id, datetime.now(UTC))
                result = "Trade completed. Your collections are updated."
            else:
                await store.trades.cancel(parts[2], callback.from_user.id, datetime.now(UTC))
                result = "Trade cancelled."
        except (TradeConflict, TradeForbidden, TradeNotFound) as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await callback.answer(result, show_alert=True)
        if callback.message and hasattr(callback.message, "edit_reply_markup"):
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                logger.info("Trade completed but its old keyboard could not be removed")

    @router.message(Command("gift"))
    async def propose_gift(message: Message, command: CommandObject) -> None:
        user = _user(message)
        target = message.reply_to_message.from_user if message.reply_to_message else None
        args = (command.args or "").split()
        if not user or not _group(message) or not target or target.is_bot or len(args) != 1:
            await respond(
                message,
                Card("Share a companion", "Reply to another player with /gift your-character-id."),
            )
            return
        if not await store.limiter.allow(f"user:{user.id}:gift", limit=10, seconds=60):
            await respond(
                message,
                Card("Take a breath", "Too many gift actions; try again shortly."),
                private=True,
            )
            return
        try:
            gift = await gifting.propose(user.id, target.id, args[0], datetime.now(UTC))
        except GiftConflict as exc:
            await respond(
                message, Card("Gift unavailable", str(exc), emoji=Emoji.WARNING), private=True
            )
            return
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=button_label(
                            "Accept gift", Emoji.GIFT, premium=settings.premium_emoji_enabled
                        ),
                        callback_data=f"gift:accept:{gift['id']}",
                        style="success",
                        icon_custom_emoji_id=Emoji.GIFT.value[1]
                        if settings.premium_emoji_enabled
                        else None,
                    ),
                    InlineKeyboardButton(
                        text=button_label(
                            "Cancel", Emoji.CANCEL, premium=settings.premium_emoji_enabled
                        ),
                        callback_data=f"gift:cancel:{gift['id']}",
                        style="danger",
                        icon_custom_emoji_id=Emoji.CANCEL.value[1]
                        if settings.premium_emoji_enabled
                        else None,
                    ),
                ]
            ]
        )
        await respond(
            message,
            Card(
                "A gift for you",
                f"{user.first_name} offered {args[0]} to {target.first_name}.\n"
                "Only the recipient can accept; the offer expires in 15 minutes.",
                emoji=Emoji.GIFT,
            ),
            markup=markup,
        )

    @router.callback_query(F.data.startswith("gift:"))
    async def gift_callback(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if (
            len(parts) != 3
            or parts[1] not in {"accept", "cancel"}
            or not re.fullmatch(r"[a-f0-9]{32}", parts[2])
        ):
            await callback.answer("Invalid gift", show_alert=True)
            return
        if not await store.limiter.allow(
            f"user:{callback.from_user.id}:gift", limit=10, seconds=60
        ):
            await callback.answer("Too many gift actions; try again shortly.", show_alert=True)
            return
        try:
            if parts[1] == "accept":
                await gifting.confirm(parts[2], callback.from_user.id, datetime.now(UTC))
                result = "Gift accepted. Your collection is updated."
            else:
                await store.gifts.cancel(parts[2], callback.from_user.id, datetime.now(UTC))
                result = "Gift cancelled."
        except (GiftConflict, GiftForbidden, GiftNotFound) as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await callback.answer(result, show_alert=True)
        if callback.message and hasattr(callback.message, "edit_reply_markup"):
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                logger.info("Gift completed but its old keyboard could not be removed")

    @router.message(Command("manage"))
    async def manage(message: Message) -> None:
        user = _user(message)
        if not user or not _group(message):
            await respond(
                message,
                Card(
                    "Manage a group",
                    "Use /manage in a group where you are an administrator.",
                    emoji=Emoji.SHIELD,
                ),
            )
            return
        try:
            await require_group_admin(message.bot, message.chat.id, user.id)
        except PermissionDenied as exc:
            await respond(
                message, Card("Admin access required", str(exc), emoji=Emoji.WARNING), private=True
            )
            return
        now = datetime.now(UTC)
        group = await store.groups.ensure(message.chat.id, message.chat.title or "Group", now)
        await store.groups.link_admin(message.chat.id, user.id, now)
        current = GroupSettings.model_validate(group["settings"])
        await respond(
            message,
            Card(
                "Group connected",
                (
                    "Open SUMMON privately to manage this group. "
                    "Use /spawns on to enable character appearances."
                ),
                emoji=Emoji.SHIELD,
                fields=(
                    ("Summons", "On" if current.spawn_enabled else "Off"),
                    ("Frequency", f"Every {current.spawn_every} messages"),
                ),
            ),
            private=True,
        )

    @router.message(Command("spawns"))
    async def spawns(message: Message, command: CommandObject) -> None:
        user = _user(message)
        if not user or not _group(message):
            return
        try:
            await require_group_admin(message.bot, message.chat.id, user.id, "can_change_info")
        except PermissionDenied as exc:
            await respond(
                message, Card("Admin access required", str(exc), emoji=Emoji.WARNING), private=True
            )
            return
        parts = (command.args or "").lower().split()
        patch: dict[str, bool | int]
        if parts in (["on"], ["off"]):
            patch = {"spawn_enabled": parts[0] == "on"}
        elif (
            len(parts) == 2
            and parts[0] == "every"
            and parts[1].isdigit()
            and 20 <= int(parts[1]) <= 10000
        ):
            patch = {"spawn_every": int(parts[1])}
        else:
            await respond(
                message,
                Card(
                    "Configure summons",
                    "Use /spawns on, /spawns off, or /spawns every 80 (20–10000).",
                    emoji=Emoji.GEAR,
                ),
                private=True,
            )
            return
        now = datetime.now(UTC)
        await store.groups.ensure(message.chat.id, message.chat.title or "Group", now)
        await store.groups.link_admin(message.chat.id, user.id, now)
        group = await store.groups.update_settings(message.chat.id, user.id, patch, now)
        await respond(
            message,
            Card(
                "Summon settings saved",
                (
                    f"Summons {'on' if group['settings']['spawn_enabled'] else 'off'} · "
                    f"every {group['settings']['spawn_every']} messages."
                ),
                emoji=Emoji.CHECK,
            ),
            private=True,
        )

    @router.message(Command("rules"))
    async def rules(message: Message) -> None:
        if not _group(message):
            return
        group = await store.groups.get(message.chat.id)
        rules_text = group["settings"].get("rules", "") if group else ""
        await respond(
            message,
            Card(
                "Group rules",
                rules_text or "This group has not set its rules yet.",
                emoji=Emoji.SHIELD,
            ),
        )

    @router.message(Command("setrules"))
    async def set_rules(message: Message, command: CommandObject) -> None:
        user = _user(message)
        if not user or not _group(message):
            return
        try:
            await require_group_admin(message.bot, message.chat.id, user.id, "can_change_info")
        except PermissionDenied as exc:
            await respond(
                message, Card("Admin access required", str(exc), emoji=Emoji.WARNING), private=True
            )
            return
        text = (command.args or "").strip()
        if not text or len(text) > 3000:
            await respond(
                message,
                Card("Set the rules", "Use /setrules followed by up to 3000 characters."),
                private=True,
            )
            return
        now = datetime.now(UTC)
        await store.groups.ensure(message.chat.id, message.chat.title or "Group", now)
        await store.groups.update_settings(message.chat.id, user.id, {"rules": text}, now)
        await respond(
            message,
            Card("Rules saved", "Members can read them with /rules.", emoji=Emoji.CHECK),
            private=True,
        )

    async def notes_rate_allowed(
        message: Message, user_id: int, action: str, *, public: bool = False
    ) -> bool:
        try:
            allowed = await store.limiter.allow(
                f"user:{user_id}:notes_{action}", limit=12 if action == "read" else 20, seconds=60
            )
            if allowed and public:
                allowed = await store.limiter.allow(
                    f"group:{message.chat.id}:notes_public", limit=40, seconds=60
                )
        except PyMongoError:
            await respond(
                message,
                Card("Notes unavailable", "Storage is unavailable; try again later."),
                private=True,
            )
            return False
        if not allowed:
            await respond(
                message,
                Card("Please slow down", "Too many note requests. Try again shortly."),
                private=True,
            )
        return bool(allowed)

    async def note_error(message: Message, exc: Exception, *, writing: bool = False) -> None:
        if isinstance(exc, (PermissionDenied, ValueError, NoteConflict)):
            text = str(exc)
        elif isinstance(exc, LookupError):
            text = (
                "The note or connected group was not found. "
                "An admin can connect the group with /manage."
            )
        elif isinstance(exc, NoteStorageError):
            text = "Stored notes need operator review."
        elif isinstance(exc, TelegramAPIError):
            text = "Could not verify current group access."
        elif isinstance(exc, PyMongoError) and writing:
            text = "Could not confirm the change. Review notes before retrying."
        else:
            text = "Could not read notes; try again later."
        await respond(message, Card("Notes unavailable", text, emoji=Emoji.WARNING), private=True)

    @router.message(Command("notes"))
    async def list_notes(message: Message) -> None:
        user = _user(message)
        if not user:
            return
        if not _group(message):
            await respond(
                message, Card("Group notes", "Use /notes in the group you want to explore.")
            )
            return
        if not await notes_rate_allowed(message, user.id, "read", public=True):
            return
        try:
            names = await GroupNotesService(message.bot, store.notes).list_for_member(
                message.chat.id, user.id
            )
        except (
            PermissionDenied,
            ValueError,
            LookupError,
            NoteStorageError,
            TelegramAPIError,
            PyMongoError,
        ) as exc:
            await note_error(message, exc)
            return
        text = (
            "\n".join(f"/note {name}" for name in names)
            if names
            else "No notes yet. A group admin can add one."
        )
        await respond(message, Card("Group notes", text, emoji=Emoji.QUESTION), standard=True)

    @router.message(Command("note"))
    async def show_note(message: Message, command: CommandObject) -> None:
        user = _user(message)
        if not user:
            return
        if not _group(message):
            await respond(message, Card("Group notes", "Use /note <name> in the relevant group."))
            return
        name = (command.args or "").strip().lower()
        if not await notes_rate_allowed(message, user.id, "read", public=True):
            return
        try:
            note = await GroupNotesService(message.bot, store.notes).get_for_member(
                message.chat.id, user.id, name
            )
        except (
            PermissionDenied,
            ValueError,
            LookupError,
            NoteStorageError,
            TelegramAPIError,
            PyMongoError,
        ) as exc:
            await note_error(message, exc)
            return
        # Preserve plain-text line breaks and escape all user-authored markup.
        await respond(
            message,
            Card(f"Note · {note['name']}", note["content"], emoji=Emoji.QUESTION),
            standard=True,
        )

    @router.message(Command("noteinfo"))
    async def note_version(message: Message, command: CommandObject) -> None:
        user = _user(message)
        if not user or not _group(message):
            return
        name = (command.args or "").strip().lower()
        if not await notes_rate_allowed(message, user.id, "read"):
            return
        try:
            note = await GroupNotesService(message.bot, store.notes).get_for_admin(
                message.chat.id, user.id, name
            )
        except (
            PermissionDenied,
            ValueError,
            LookupError,
            NoteStorageError,
            TelegramAPIError,
            PyMongoError,
        ) as exc:
            await note_error(message, exc)
            return
        await respond(
            message,
            Card(
                f"Note revision · {name}",
                "Use this revision with /editnote or /delnote. Notes are public in group messages.",
                fields=(("Revision", note["revision"]),),
            ),
            private=True,
        )

    @router.message(Command("savenote"))
    async def save_note(message: Message, command: CommandObject) -> None:
        user = _user(message)
        if not user or not _group(message):
            return
        parts = (command.args or "").split(maxsplit=1)
        if len(parts) != 2:
            await respond(
                message,
                Card(
                    "Create a note", "Use /savenote <name> <text>. Notes are public to the group."
                ),
                private=True,
            )
            return
        if not await notes_rate_allowed(message, user.id, "write"):
            return
        try:
            await GroupNotesService(message.bot, store.notes).save(
                message.chat.id, user.id, parts[0].lower(), parts[1], None, datetime.now(UTC)
            )
        except (
            PermissionDenied,
            ValueError,
            LookupError,
            NoteConflict,
            NoteStorageError,
            TelegramAPIError,
            PyMongoError,
        ) as exc:
            await note_error(message, exc, writing=True)
            return
        await respond(
            message,
            Card(
                "Note created",
                "Members can read it with /note. Use /noteinfo to edit or remove it.",
                emoji=Emoji.CHECK,
            ),
            private=True,
        )

    @router.message(Command("editnote"))
    async def edit_note(message: Message, command: CommandObject) -> None:
        user = _user(message)
        if not user or not _group(message):
            return
        parts = (command.args or "").split(maxsplit=2)
        if len(parts) != 3:
            await respond(
                message,
                Card(
                    "Edit a note",
                    "Use /editnote <name> <revision> <text>. "
                    "Get the current revision with /noteinfo <name>.",
                ),
                private=True,
            )
            return
        if not await notes_rate_allowed(message, user.id, "write"):
            return
        try:
            await GroupNotesService(message.bot, store.notes).save(
                message.chat.id, user.id, parts[0].lower(), parts[2], parts[1], datetime.now(UTC)
            )
        except (
            PermissionDenied,
            ValueError,
            LookupError,
            NoteConflict,
            NoteStorageError,
            TelegramAPIError,
            PyMongoError,
        ) as exc:
            await note_error(message, exc, writing=True)
            return
        await respond(
            message,
            Card("Note updated", "Members can read the new text with /note.", emoji=Emoji.CHECK),
            private=True,
        )

    @router.message(Command("delnote"))
    async def delete_note(message: Message, command: CommandObject) -> None:
        user = _user(message)
        if not user or not _group(message):
            return
        parts = (command.args or "").split()
        if len(parts) != 3 or not re.fullmatch(r"[a-f0-9]{32}", parts[1]) or parts[2] != "confirm":
            await respond(
                message,
                Card(
                    "Confirm note removal",
                    "Use /delnote <name> <revision> confirm. "
                    "Get the current revision with /noteinfo <name>.",
                ),
                private=True,
            )
            return
        if not await notes_rate_allowed(message, user.id, "write"):
            return
        try:
            await GroupNotesService(message.bot, store.notes).delete(
                message.chat.id, user.id, parts[0].lower(), parts[1], datetime.now(UTC)
            )
        except (
            PermissionDenied,
            ValueError,
            LookupError,
            NoteConflict,
            NoteStorageError,
            TelegramAPIError,
            PyMongoError,
        ) as exc:
            await note_error(message, exc, writing=True)
            return
        await respond(
            message,
            Card(
                "Note removed",
                "The removal is recorded in the group activity log.",
                emoji=Emoji.CHECK,
            ),
            private=True,
        )

    @router.message(Command("ban", "unban", "mute", "unmute", "kick", "warn"))
    async def moderate(message: Message, command: CommandObject) -> None:
        user = _user(message)
        target = message.reply_to_message.from_user if message.reply_to_message else None
        if not user or not _group(message) or not target or target.is_bot:
            await respond(
                message,
                Card(
                    "Choose a member",
                    "Reply to a member's message with the moderation command.",
                    emoji=Emoji.SHIELD,
                ),
                private=True,
            )
            return
        parts = (command.args or "").split()
        duration = _duration(parts[0]) if parts and command.command in {"ban", "mute"} else None
        reason = " ".join(parts[1:] if duration else parts)
        try:
            result = await ModerationService(message.bot, store.groups).act(
                message.chat.id,
                user.id,
                target.id,
                command.command,
                reason,
                datetime.now(UTC),
                duration,
            )
        except (PermissionDenied, ValueError) as exc:
            await respond(
                message, Card("Action not allowed", str(exc), emoji=Emoji.WARNING), private=True
            )
            return
        suffix = (
            f" Warnings: {result['warning_count']}."
            if result.get("warning_count") is not None
            else ""
        )
        await respond(
            message,
            Card(
                "Moderation recorded",
                f"{command.command.title()} applied to {target.first_name}.{suffix}",
                emoji=Emoji.SHIELD,
            ),
        )

    async def warning_rate_allowed(message: Message, actor_id: int, action: str) -> bool:
        try:
            allowed = await store.limiter.allow(
                f"user:{actor_id}:{action}",
                limit=30 if action == "warning_review" else 20,
                seconds=60,
            )
        except PyMongoError:
            await respond(
                message,
                Card("Warnings unavailable", "Storage is unavailable; try the review later."),
                private=True,
            )
            return False
        if not allowed:
            await respond(
                message,
                Card("Please slow down", "Too many warning requests. Try again shortly."),
                private=True,
            )
        return bool(allowed)

    def warning_reply_target(message: Message) -> Any | None:
        reply = message.reply_to_message
        if not reply or getattr(reply, "sender_chat", None):
            return None
        target = reply.from_user
        return target if target and not target.is_bot else None

    @router.message(Command("warnings"))
    async def review_warnings(message: Message, command: CommandObject) -> None:
        user = _user(message)
        target = warning_reply_target(message)
        if not user or not _group(message) or not target:
            await respond(
                message,
                Card("Choose a member", "Reply to a member's message with /warnings."),
                private=True,
            )
            return
        before = (command.args or "").strip() or None
        if before is not None and not re.fullmatch(r"[a-f0-9]{32}", before):
            await respond(
                message, Card("Invalid cursor", "Use the cursor from /warnings."), private=True
            )
            return
        if not await warning_rate_allowed(message, user.id, "warning_review"):
            return
        try:
            page = await ModerationService(message.bot, store.groups).list_warnings(
                message.chat.id, user.id, target.id, before=before, limit=5
            )
        except (PermissionDenied, ValueError, LookupError) as exc:
            await respond(message, Card("Cannot review warnings", str(exc)), private=True)
            return
        except (TelegramAPIError, PyMongoError):
            await respond(
                message,
                Card("Warnings unavailable", "Could not verify access or read warning records."),
                private=True,
            )
            return
        fields = tuple(
            (
                f"ID {item['id']}",
                f"{item.get('reason') or 'No reason provided.'}\n"
                f"Issued by {item['actor_id']} · {item['created_at'].isoformat()}",
            )
            for item in page["items"]
        )
        cursor = page.get("next_cursor")
        details = (
            ("Older warnings", f"Reply to the member with /warnings {cursor}.") if cursor else None
        )
        await respond(
            message,
            Card(
                "Active warnings",
                f"Member {target.id} has {page['active_count']} active warnings. "
                "No automatic penalty is applied.",
                emoji=Emoji.SHIELD,
                fields=fields,
                details=details,
            ),
            private=True,
        )

    @router.message(Command("unwarn"))
    async def unwarn(message: Message, command: CommandObject) -> None:
        user = _user(message)
        target = warning_reply_target(message)
        if not user or not _group(message) or not target:
            await respond(
                message,
                Card("Choose a member", "Reply to the warned member's message with /unwarn."),
                private=True,
            )
            return
        parts = (command.args or "").split(maxsplit=2)
        if len(parts) < 2 or not re.fullmatch(r"[a-f0-9]{32}", parts[0]) or parts[1] != "confirm":
            await respond(
                message,
                Card("Confirm the correction", "Reply with /unwarn <warning ID> confirm [reason]."),
                private=True,
            )
            return
        if not await warning_rate_allowed(message, user.id, "warning_revoke"):
            return
        try:
            await ModerationService(message.bot, store.groups).revoke_warning(
                message.chat.id,
                user.id,
                target.id,
                parts[0],
                parts[2] if len(parts) > 2 else "",
                datetime.now(UTC),
            )
        except (PermissionDenied, ValueError, LookupError) as exc:
            await respond(message, Card("Cannot revoke warning", str(exc)), private=True)
            return
        except TelegramAPIError:
            await respond(
                message,
                Card("Cannot verify access", "Current group permissions are unavailable."),
                private=True,
            )
            return
        except PyMongoError:
            await respond(
                message,
                Card(
                    "Result uncertain",
                    "Could not confirm the correction. Review warnings before retrying.",
                ),
                private=True,
            )
            return
        await respond(
            message,
            Card(
                "Warning revoked",
                "The correction is recorded; Telegram restrictions were not changed.",
                emoji=Emoji.CHECK,
            ),
            private=True,
        )

    @router.message(F.new_chat_members)
    async def welcome(message: Message) -> None:
        if not _group(message):
            return
        group = await store.groups.get(message.chat.id)
        if not group:
            return
        config = GroupSettings.model_validate(group["settings"])
        if config.welcome_enabled:
            names = ", ".join(
                user.first_name for user in (message.new_chat_members or []) if not user.is_bot
            )
            if names:
                await respond(
                    message, Card(f"Welcome, {names}", config.welcome_text, emoji=Emoji.SPARK)
                )

    @router.message(F.left_chat_member)
    async def goodbye(message: Message) -> None:
        departed = message.left_chat_member
        if not _group(message) or not departed or departed.is_bot:
            return
        group = await store.groups.get(message.chat.id)
        if not group:
            return
        config = GroupSettings.model_validate(group["settings"])
        if not config.goodbye_enabled:
            return
        # Bound service-event amplification per member and per group. These are
        # shared fixed windows; an event can fall across a bucket boundary.
        if not await store.limiter.allow(
            f"group:{message.chat.id}:goodbye:{departed.id}", limit=1, seconds=60
        ):
            return
        if not await store.limiter.allow(f"group:{message.chat.id}:goodbye", limit=10, seconds=60):
            return
        await respond(
            message,
            Card(f"Goodbye, {departed.first_name}", config.goodbye_text, emoji=Emoji.MOON),
        )

    @router.message(F.chat.type.in_({"group", "supergroup"}))
    async def group_activity(message: Message) -> None:
        user = _user(message)
        bot = message.bot
        if not user or bot is None or not is_group_user_content(message):
            return
        # Ephemeral inputs and service events are not public group activity.
        if message.ephemeral_message_id is not None:
            return
        now = datetime.now(UTC)
        await store.groups.ensure(message.chat.id, message.chat.title or "Group", now)
        event = await collection.on_message(
            message.chat.id, message.chat.title or "Group", user.id, now
        )
        if not event:
            return
        character = event["character"]
        card = Card(
            "A character appears",
            "A new companion is nearby. Guess their name with /summon name before they disappear!",
            emoji=Emoji.SPARK,
            fields=(("Series", character["category"]), ("Rarity", character["rarity"].title())),
        )
        image = character.get("image_url", "")
        if image.startswith("/art/"):
            image = settings.webapp_url.rstrip("/") + image if settings.webapp_url else ""
        if not image:
            await store.collection.mark_delivery_failed(message.chat.id, event["spawn"]["id"])
            logger.warning("Spawn has no public artwork URL; spawn marked failed")
            return
        try:
            await send_photo_card(
                bot,
                message.chat.id,
                image,
                card,
                premium=settings.premium_emoji_enabled,
                message_thread_id=message.message_thread_id,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            # A text-only challenge provides no character artwork to guess.
            # Cancel only after a definite rejection; an ambiguous network
            # failure may have delivered, so leave that reservation to expire.
            await store.collection.mark_delivery_failed(message.chat.id, event["spawn"]["id"])
            logger.warning("Spawn artwork rejected; spawn marked failed")

    @router.edited_message()
    async def ignore_edited_message(message: Message) -> None:
        # The outer middleware checks content; never execute edited commands or
        # count an edit as new group activity.
        return None

    return router
