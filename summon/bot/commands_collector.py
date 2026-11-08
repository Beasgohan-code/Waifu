"""Compose collector features with the existing native Wzgram client."""

from wzgram import Client

from summon.api.context import Store
from summon.bot.modules.context import CollectorContext, Responder
from summon.bot.modules.registry import register_collector_modules
from summon.config import Settings
from summon.services.collection import CollectionService


def register_collector_commands(
    app: Client,
    store: Store,
    settings: Settings,
    collection: CollectionService,
    respond: Responder,
) -> None:
    register_collector_modules(CollectorContext(app, store, settings, collection, respond))
