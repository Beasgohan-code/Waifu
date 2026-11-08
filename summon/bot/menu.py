"""Telegram command menus; personal group actions use private overlays."""

from __future__ import annotations

from wzgram.types import BotCommand

from summon.bot.presentation import Emoji


def _command(name: str, description: str, icon: Emoji) -> BotCommand:
    # Command descriptions have no custom-emoji entities; use the matching
    # Unicode fallback rather than inserting unsupported HTML into the menu.
    return BotCommand(command=name, description=f"{icon.value[0]} {description}")


COMMANDS = [
    _command("start", "Open SUMMON", Emoji.SUMMON_BRAND),
    _command("help", "Explore commands", Emoji.QUESTION),
    _command("ping", "Check that the bot responds", Emoji.CHECK),
    _command("pinfo", "Check your Telegram Premium indication", Emoji.STAR),
    _command("summon", "Guess a character in your group", Emoji.SPARK),
    _command("collection", "View your collection", Emoji.OPEN_COLLECTION),
    _command("search", "Search characters by name", Emoji.SEARCH),
    _command("check", "See a character by ID", Emoji.SEARCH),
    _command("chancelist", "Show summon rarity weights", Emoji.RARITY_STAR),
    _command("favorites", "View your favorite characters", Emoji.HEART),
    _command("favorite", "Add or remove an owned favorite", Emoji.HEART),
    _command("achievements", "View your achievements", Emoji.CROWN),
    _command("profile", "View your collector profile", Emoji.STAR),
    _command("balance", "View your in-game points", Emoji.COIN),
    _command("daily", "Claim your daily reward", Emoji.GIFT_ACTION),
    _command("streak", "Check your daily streak", Emoji.HOURGLASS),
    _command("stats", "View aggregate community counts", Emoji.STATS),
    _command("trade", "Offer a character trade", Emoji.TRADE),
    _command("gift", "Offer a character as a gift", Emoji.GIFT_ACTION),
    _command("top", "View the collectors leaderboard", Emoji.CROWN),
    _command("manage", "Connect a group you administer", Emoji.ADMIN_SHIELD),
    _command("checkspawn", "Check your group's summon status", Emoji.SEARCH),
    _command("rules", "Read group rules", Emoji.RULES),
    _command("notes", "List the group's public notes", Emoji.NOTE),
    _command("note", "Read a group note by name", Emoji.NOTE),
]


def group_commands() -> list[BotCommand]:
    """Use normal menu entries; private group results are sent as overlays or DMs."""
    # Wzgram's `ephemeral` command flag removes a menu entry after use; it is not
    # the private response flag and must not be used to imply confidentiality.
    return [BotCommand(command=item.command, description=item.description) for item in COMMANDS]
