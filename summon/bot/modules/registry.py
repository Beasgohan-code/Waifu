"""Explicit collector-feature registration; never import arbitrary files at runtime."""

from collections.abc import Callable

from summon.bot.modules import collection, daily, favorites, information, progression, search, start
from summon.bot.modules.context import CollectorContext

# Order is intentional: Wzgram selects at most one matching handler per group.
# These modules register against the existing worker; none creates a new client.
COLLECTOR_MODULES: tuple[Callable[[CollectorContext], None], ...] = (
    start.register,
    collection.register,
    information.register,
    search.register,
    favorites.register,
    progression.register,
    daily.register,
    progression.register_leaderboard,
)


def register_collector_modules(context: CollectorContext) -> None:
    for register in COLLECTOR_MODULES:
        register(context)
