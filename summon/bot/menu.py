"""Telegram command menus; personal group actions use private overlays."""

from __future__ import annotations

from wzgram.types import BotCommand

COMMANDS = [
    BotCommand(command="start", description="Open SUMMON"),
    BotCommand(command="help", description="Explore commands"),
    BotCommand(command="ping", description="Check that the bot responds"),
    BotCommand(command="pinfo", description="Check your Telegram Premium indication"),
    BotCommand(command="summon", description="Guess a character in your group"),
    BotCommand(command="collection", description="View your collection"),
    BotCommand(command="search", description="Search characters by name"),
    BotCommand(command="check", description="See a character by ID"),
    BotCommand(command="chancelist", description="Show summon rarity weights"),
    BotCommand(command="favorites", description="View your favorite characters"),
    BotCommand(command="favorite", description="Add or remove an owned favorite"),
    BotCommand(command="achievements", description="View your achievements"),
    BotCommand(command="profile", description="View your collector profile"),
    BotCommand(command="balance", description="View your in-game points"),
    BotCommand(command="daily", description="Claim your daily reward"),
    BotCommand(command="streak", description="Check your daily streak"),
    BotCommand(command="stats", description="View aggregate community counts"),
    BotCommand(command="trade", description="Offer a character trade"),
    BotCommand(command="gift", description="Offer a character as a gift"),
    BotCommand(command="top", description="View the collectors leaderboard"),
    BotCommand(command="manage", description="Connect a group you administer"),
    BotCommand(command="checkspawn", description="Check your group's summon status"),
    BotCommand(command="rules", description="Read group rules"),
    BotCommand(command="notes", description="List the group's public notes"),
    BotCommand(command="note", description="Read a group note by name"),
]


def group_commands() -> list[BotCommand]:
    """Use normal menu entries; private group results are sent as overlays or DMs."""
    # Wzgram's `ephemeral` command flag removes a menu entry after use; it is not
    # the private response flag and must not be used to imply confidentiality.
    return [BotCommand(command=item.command, description=item.description) for item in COMMANDS]
