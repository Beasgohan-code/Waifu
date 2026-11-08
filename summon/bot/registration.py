"""Verify the ordered native handler manifest before connecting Telegram.

Wzgram runs only the first matching handler in a group, so merely counting
handlers cannot detect a missing lease guard or an early catch-all observer.
This detects accidental registration changes, not correctness of filters,
Telegram delivery or protection from an attacker who can modify the code.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any

from wzgram import Client
from wzgram.handlers import CallbackQueryHandler, EditedMessageHandler, MessageHandler

HandlerSpec = tuple[type[Any], str, str]


def _specs(module: str, entries: Iterable[tuple[type[Any], str]]) -> tuple[HandlerSpec, ...]:
    return tuple((handler_type, module, name) for handler_type, name in entries)


M, E, C = MessageHandler, EditedMessageHandler, CallbackQueryHandler

# Keep in the same order as registration. Wzgram's group -1000 blocks updates
# after lease loss; group -100 applies protection before commands in group 0.
EXPECTED_HANDLERS: dict[int, tuple[HandlerSpec, ...]] = {
    -1000: _specs(
        "summon.bot.ingress",
        ((M, "guard_message"), (E, "guard_edit"), (C, "guard_callback")),
    ),
    -100: _specs("summon.bot.protection", ((M, "check_message"), (E, "check_edit"))),
    0: (
        *_specs(
            "summon.bot.modules.catalog_admin",
            (
                (M, "operator_panel"),
                (M, "operator_list"),
                (M, "change_spawn_weight"),
                (M, "create_character"),
                (C, "upload_workflow"),
                (M, "edit_character"),
            ),
        ),
        *_specs(
            "summon.bot.modules.start",
            ((M, "start"), (C, "start_guide"), (M, "help_command")),
        ),
        *_specs(
            "summon.bot.modules.collection",
            (
                (M, "guess"),
                (M, "show_collection"),
                (M, "collection_display_mode"),
                (C, "collection_mode_callback"),
            ),
        ),
        *_specs(
            "summon.bot.modules.information",
            (
                (M, "ping"),
                (M, "premium_info"),
                (M, "character_info"),
                (M, "community_stats"),
                (M, "rarity_weights"),
            ),
        ),
        *_specs("summon.bot.modules.search", ((M, "search_characters"), (C, "search_page"))),
        *_specs("summon.bot.modules.favorites", ((M, "show_favorites"), (M, "update_favorite"))),
        *_specs(
            "summon.bot.modules.progression",
            ((M, "show_achievements"), (M, "profile"), (M, "balance"), (M, "streak")),
        ),
        *_specs("summon.bot.modules.daily", ((M, "daily_character"), (M, "daily"))),
        *_specs("summon.bot.modules.progression", ((M, "leaderboard"),)),
        *_specs(
            "summon.bot.modules.exchange",
            (
                (M, "propose_trade"),
                (C, "trade_callback"),
                (M, "propose_gift"),
                (C, "gift_callback"),
            ),
        ),
        *_specs(
            "summon.bot.modules.groups",
            (
                (M, "manage"),
                (M, "spawns"),
                (M, "check_spawn"),
                (M, "group_stats"),
                (M, "rules"),
                (M, "set_rules"),
                (M, "list_notes"),
                (M, "show_note"),
                (M, "note_version"),
                (M, "save_note"),
                (M, "edit_note"),
                (M, "delete_note"),
                (M, "moderate"),
                (M, "review_warnings"),
                (M, "unwarn"),
            ),
        ),
        *_specs(
            "summon.bot.modules.group_events",
            (
                (M, "welcome"),
                (M, "goodbye"),
                (M, "manual_spawn"),
                (M, "group_activity"),
                (E, "ignore_edited_message"),
            ),
        ),
    ),
}


def validate_handler_registration(app: Client) -> None:
    """Fail closed if a handler is missing, duplicated, unfiltered or reordered."""
    groups = app.dispatcher.groups
    if tuple(groups) != tuple(EXPECTED_HANDLERS):
        raise RuntimeError("Native Wzgram handler groups are incomplete or unexpected")
    for group, expected in EXPECTED_HANDLERS.items():
        actual = groups[group]
        if len(actual) != len(expected):
            raise RuntimeError(f"Native Wzgram handler registration mismatch in group {group}")
        for handler, (handler_type, module, name) in zip(actual, expected, strict=True):
            callback = getattr(handler, "callback", None)
            # The lease callback guard intentionally applies to every callback.
            has_filter = not (group == -1000 and name == "guard_callback")
            if (
                type(handler) is not handler_type
                or getattr(callback, "__module__", None) != module
                or getattr(callback, "__name__", None) != name
                or not inspect.iscoroutinefunction(callback)
                or (getattr(handler, "filters", None) is None) == has_filter
            ):
                raise RuntimeError(f"Native Wzgram handler registration mismatch in group {group}")
