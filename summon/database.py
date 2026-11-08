"""Async MongoDB lifecycle, schema validation and indexes.

Ownership/economy mutations use multi-document transactions, so production must
use a replica set or sharded cluster. No non-transactional fallback is provided.
"""

from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.errors import (
    CollectionInvalid,
    OperationFailure,
    PyMongoError,
    ServerSelectionTimeoutError,
)

from summon.auto_replies import (
    AUTO_REPLY_NAME_PATTERN,
    AUTO_REPLY_REVISION_PATTERN,
    MAX_AUTO_REPLIES,
    MAX_REPLY_LENGTH,
    MAX_TRIGGER_LENGTH,
)
from summon.config import Settings
from summon.group_notes import MAX_NOTE_LENGTH, MAX_NOTES, NOTE_NAME_PATTERN, NOTE_REVISION_PATTERN
from summon.rarities import MAX_SPAWN_WEIGHT, RARITY_VALUES, RARITY_WEIGHTS

_NUMBER = ["int", "long"]
COLLECTION_SCHEMAS: dict[str, dict[str, Any]] = {
    "characters": {
        "bsonType": "object",
        "required": ["_id", "name", "name_key", "category", "rarity", "active", "random_key"],
        "properties": {
            "_id": {"bsonType": "string"},
            "name": {"bsonType": "string", "minLength": 1, "maxLength": 120},
            "name_key": {"bsonType": "string"},
            "category": {"bsonType": "string", "maxLength": 120},
            "rarity": {"enum": list(RARITY_VALUES)},
            "number": {"bsonType": _NUMBER, "minimum": 1, "maximum": 2**53 - 1},
            "media_type": {"enum": ["photo", "video", "animation"]},
            "telegram_file_id": {"bsonType": ["string", "null"], "maxLength": 2048},
            "active": {"bsonType": "bool"},
            "random_key": {"bsonType": "double", "minimum": 0, "maximum": 1},
        },
    },
    "catalog_counters": {
        "bsonType": "object",
        "required": ["_id", "last_number"],
        "additionalProperties": False,
        "properties": {
            "_id": {"enum": ["characters"]},
            "last_number": {"bsonType": _NUMBER, "minimum": 0, "maximum": 2**53 - 1},
        },
    },
    "spawn_weights": {
        "bsonType": "object",
        "required": ["_id", "weight", "updated_by", "updated_at"],
        "additionalProperties": False,
        "properties": {
            "_id": {"enum": list(RARITY_WEIGHTS)},
            "weight": {"bsonType": _NUMBER, "minimum": 0, "maximum": MAX_SPAWN_WEIGHT},
            "updated_by": {"bsonType": _NUMBER, "minimum": 1, "maximum": 2**53 - 1},
            "updated_at": {"bsonType": "date"},
        },
    },
    "character_artworks": {
        "bsonType": "object",
        "required": ["_id", "content", "content_type", "created_at"],
        "additionalProperties": False,
        "properties": {
            "_id": {"bsonType": "string", "pattern": "^[0-9a-f]{64}$"},
            "content": {"bsonType": "binData"},
            "content_type": {"enum": ["image/jpeg"]},
            "created_at": {"bsonType": "date"},
        },
    },
    "inventory": {
        "bsonType": "object",
        "required": ["user_id", "character_id", "quantity"],
        "properties": {
            "user_id": {"bsonType": _NUMBER},
            "character_id": {"bsonType": "string"},
            "quantity": {"bsonType": _NUMBER, "minimum": 0},
            "favorite": {"bsonType": "bool"},
        },
    },
    "groups": {
        "bsonType": "object",
        "required": ["_id", "title", "settings", "message_count"],
        "properties": {
            "_id": {"bsonType": _NUMBER},
            "title": {"bsonType": "string", "maxLength": 255},
            "settings": {"bsonType": "object"},
            "message_count": {"bsonType": _NUMBER, "minimum": 0},
            "auto_replies_v1": {
                "bsonType": "object",
                "maxProperties": MAX_AUTO_REPLIES,
                "additionalProperties": False,
                "patternProperties": {
                    AUTO_REPLY_NAME_PATTERN: {
                        "bsonType": "object",
                        "required": [
                            "trigger",
                            "response",
                            "enabled",
                            "revision",
                            "created_at",
                            "created_by",
                            "updated_at",
                            "updated_by",
                        ],
                        "additionalProperties": False,
                        "properties": {
                            "trigger": {
                                "bsonType": "string",
                                "minLength": 2,
                                "maxLength": MAX_TRIGGER_LENGTH,
                            },
                            "response": {
                                "bsonType": "string",
                                "minLength": 1,
                                "maxLength": MAX_REPLY_LENGTH,
                            },
                            "enabled": {"bsonType": "bool"},
                            "revision": {
                                "bsonType": "string",
                                "pattern": AUTO_REPLY_REVISION_PATTERN,
                            },
                            "created_at": {"bsonType": "date"},
                            "created_by": {"bsonType": _NUMBER, "minimum": 1},
                            "updated_at": {"bsonType": "date"},
                            "updated_by": {"bsonType": _NUMBER, "minimum": 1},
                        },
                    }
                },
            },
            "saved_notes_v1": {
                "bsonType": "object",
                "maxProperties": MAX_NOTES,
                "additionalProperties": False,
                "patternProperties": {
                    NOTE_NAME_PATTERN: {
                        "bsonType": "object",
                        "required": [
                            "content",
                            "revision",
                            "created_at",
                            "created_by",
                            "updated_at",
                            "updated_by",
                        ],
                        "properties": {
                            "content": {
                                "bsonType": "string",
                                "minLength": 1,
                                "maxLength": MAX_NOTE_LENGTH,
                            },
                            "revision": {"bsonType": "string", "pattern": NOTE_REVISION_PATTERN},
                            "created_at": {"bsonType": "date"},
                            "created_by": {"bsonType": _NUMBER, "minimum": 1},
                            "updated_at": {"bsonType": "date"},
                            "updated_by": {"bsonType": _NUMBER, "minimum": 1},
                        },
                    }
                },
            },
        },
    },
    "web_sessions": {
        "bsonType": "object",
        "required": ["_id", "user_id", "expires_at"],
        "properties": {
            "_id": {"bsonType": "string", "pattern": "^[0-9a-f]{64}$"},
            "user_id": {"bsonType": _NUMBER, "minimum": 1},
            "expires_at": {"bsonType": "date"},
        },
    },
    "gifts": {
        "bsonType": "object",
        "required": ["_id", "sender_id", "receiver_id", "character_id", "status", "expires_at"],
        "properties": {
            "_id": {"bsonType": "string"},
            "sender_id": {"bsonType": _NUMBER},
            "receiver_id": {"bsonType": _NUMBER},
            "character_id": {"bsonType": "string"},
            "status": {"enum": ["pending", "completed", "cancelled", "expired"]},
            "expires_at": {"bsonType": "date"},
        },
    },
    "trades": {
        "bsonType": "object",
        "required": [
            "_id",
            "sender_id",
            "receiver_id",
            "offer_id",
            "request_id",
            "status",
            "expires_at",
        ],
        "properties": {
            "_id": {"bsonType": "string"},
            "sender_id": {"bsonType": _NUMBER},
            "receiver_id": {"bsonType": _NUMBER},
            "offer_id": {"bsonType": "string"},
            "request_id": {"bsonType": "string"},
            "status": {"enum": ["pending", "completed", "cancelled", "expired"]},
            "expires_at": {"bsonType": "date"},
        },
    },
}

# Every list/read path is bounded and uses one of these indexes or the _id index.
INDEXES: dict[str, list[tuple[list[tuple[str, int]], dict[str, Any]]]] = {
    "characters": [
        (
            [("number", ASCENDING)],
            {"unique": True, "partialFilterExpression": {"number": {"$exists": True}}},
        ),
        ([("active", ASCENDING), ("name_key", ASCENDING), ("_id", ASCENDING)], {}),
        (
            [
                ("active", ASCENDING),
                ("rarity", ASCENDING),
                ("name_key", ASCENDING),
                ("_id", ASCENDING),
            ],
            {},
        ),
        (
            [
                ("active", ASCENDING),
                ("category", ASCENDING),
                ("name_key", ASCENDING),
                ("_id", ASCENDING),
            ],
            {},
        ),
        (
            [
                ("active", ASCENDING),
                ("rarity", ASCENDING),
                ("category", ASCENDING),
                ("name_key", ASCENDING),
                ("_id", ASCENDING),
            ],
            {},
        ),
        ([("active", ASCENDING), ("rarity", ASCENDING), ("_id", ASCENDING)], {}),
        # Retained for compatibility; spawn selection no longer uses fixed random-key pivots.
        ([("active", ASCENDING), ("rarity", ASCENDING), ("random_key", ASCENDING)], {}),
        ([("active", ASCENDING), ("random_key", ASCENDING)], {}),
    ],
    "inventory": [
        ([("user_id", ASCENDING), ("character_id", ASCENDING)], {"unique": True}),
        ([("user_id", ASCENDING), ("acquired_at", DESCENDING), ("character_id", ASCENDING)], {}),
        ([("user_id", ASCENDING), ("quantity", DESCENDING), ("character_id", ASCENDING)], {}),
        (
            [
                ("user_id", ASCENDING),
                ("favorite", ASCENDING),
                ("acquired_at", DESCENDING),
                ("character_id", ASCENDING),
            ],
            {},
        ),
    ],
    "users": [([("catch_count", DESCENDING), ("_id", ASCENDING)], {})],
    "gifts": [
        ([("sender_id", ASCENDING), ("created_at", DESCENDING)], {}),
        ([("receiver_id", ASCENDING), ("created_at", DESCENDING)], {}),
    ],
    "trades": [
        ([("sender_id", ASCENDING), ("created_at", DESCENDING)], {}),
        ([("receiver_id", ASCENDING), ("created_at", DESCENDING)], {}),
    ],
    "spawns": [([("group_id", ASCENDING), ("created_at", DESCENDING)], {})],
    "activity": [
        ([("group_id", ASCENDING), ("created_at", DESCENDING)], {}),
        ([("user_id", ASCENDING), ("created_at", DESCENDING)], {}),
    ],
    "group_admins": [([("user_id", ASCENDING), ("group_id", ASCENDING)], {"unique": True})],
    "warnings": [
        ([("group_id", ASCENDING), ("user_id", ASCENDING), ("created_at", DESCENDING)], {}),
        (
            [
                ("group_id", ASCENDING),
                ("user_id", ASCENDING),
                ("revoked_at", ASCENDING),
                ("created_at", DESCENDING),
                ("_id", DESCENDING),
            ],
            {},
        ),
    ],
    "wishlists": [([("user_id", ASCENDING), ("character_id", ASCENDING)], {"unique": True})],
    "reward_history": [([("user_id", ASCENDING), ("created_at", DESCENDING)], {})],
    "rate_limits": [([("expires_at", ASCENDING)], {"expireAfterSeconds": 0})],
    "auth_nonces": [([("expires_at", ASCENDING)], {"expireAfterSeconds": 0})],
    "web_sessions": [([("expires_at", ASCENDING)], {"expireAfterSeconds": 0})],
}


# Optional and separately named so enabling monitoring is an explicit schema
# change. It stores no Telegram update content or raw credential.
POLLER_HEALTH_SCHEMA: dict[str, Any] = {
    "bsonType": "object",
    "required": ["_id", "observed_at", "expires_at"],
    "additionalProperties": False,
    "properties": {
        "_id": {"bsonType": "string", "pattern": "^(poller|mtproto):[0-9a-f]{64}$"},
        "observed_at": {"bsonType": "date"},
        "expires_at": {"bsonType": "date"},
    },
}
POLLER_HEALTH_INDEXES: list[tuple[list[tuple[str, int]], dict[str, Any]]] = [
    ([("expires_at", ASCENDING)], {"expireAfterSeconds": 0})
]


async def initialize_database(database: Any, *, include_poller_health: bool = False) -> None:
    schemas = dict(COLLECTION_SCHEMAS)
    indexes_by_collection = dict(INDEXES)
    if include_poller_health:
        schemas["summon_poller_heartbeats_v1"] = POLLER_HEALTH_SCHEMA
        indexes_by_collection["summon_poller_heartbeats_v1"] = POLLER_HEALTH_INDEXES
    existing = set(await database.list_collection_names())
    for name, schema in schemas.items():
        validator = {"$jsonSchema": schema}
        if name not in existing:
            try:
                await database.create_collection(name, validator=validator)
                continue
            except CollectionInvalid:  # Another instance created it concurrently.
                pass
        await database.command("collMod", name, validator=validator, validationLevel="moderate")
    for name, indexes in indexes_by_collection.items():
        for keys, options in indexes:
            await database[name].create_index(keys, **options)


class DatabaseTopologyError(RuntimeError):
    """The connected deployment cannot be used for transactional writes."""


class DatabaseStartupError(RuntimeError):
    """A redacted runtime-startup failure; server details stay out of logs."""


def safe_startup_error(exc: PyMongoError) -> DatabaseStartupError:
    """Provide guidance without echoing driver messages, hosts or credentials.

    The driver may include server addresses and other sensitive connection
    details in its message. Inspect fixed markers only; callers must raise the
    resulting error with `from None` to suppress the original exception chain.
    """
    if isinstance(exc, ServerSelectionTimeoutError):
        detail = str(exc)[:16384].upper()
        if "CERTIFICATE_VERIFY_FAILED" in detail:
            return DatabaseStartupError(
                "MongoDB TLS certificate verification failed. Check the cluster's certificate "
                "and the runtime's trusted CA configuration; do not disable TLS verification."
            )
        if "SSL HANDSHAKE FAILED" in detail or "TLSV1_ALERT_INTERNAL_ERROR" in detail:
            return DatabaseStartupError(
                "MongoDB TLS handshake failed during server selection. Check the cluster is "
                "active, its project IP access list includes all hosting-service outbound IP "
                "ranges, and routing and TLS settings are correct. The cause cannot be "
                "determined from this alert alone; do not disable TLS verification."
            )
        return DatabaseStartupError(
            "MongoDB server selection timed out. Check the cluster is running, the "
            "service's outbound IP access list, network routing, URI and TLS settings. "
            "The cause cannot be determined from the timeout alone."
        )
    if isinstance(exc, OperationFailure) and exc.code in {13, 18}:
        return DatabaseStartupError(
            "MongoDB authentication or authorization failed. Check the configured database "
            "user and permissions, without sharing credentials."
        )
    return DatabaseStartupError(
        "MongoDB startup failed. Check the URI, network access, TLS, cluster status "
        "and database-user permissions."
    )


async def require_transaction_topology(client: Any) -> None:
    """Reject clearly unsupported deployments before schema initialization.

    This read-only check is a necessary precondition, not proof that a real
    transaction, majority write, or application-specific permission will work.
    PyMongo 4.19 supports MongoDB 4.4+ (wire version 9+).
    """
    try:
        hello = await client.admin.command("hello")
    except OperationFailure as exc:
        # MongoDB 4.4 may not recognize `hello`. Never bypass authorization or
        # network failures; only an explicit CommandNotFound permits fallback.
        if exc.code != 59:
            raise
        hello = await client.admin.command("isMaster")

    wire = hello.get("maxWireVersion")
    timeout = hello.get("logicalSessionTimeoutMinutes")
    set_name = hello.get("setName")
    replica_primary = (
        isinstance(set_name, str)
        and bool(set_name)
        and (hello.get("isWritablePrimary") is True or hello.get("ismaster") is True)
    )
    mongos = hello.get("msg") == "isdbgrid"
    # In load-balanced mode PyMongo requires a serviceId during the initial
    # handshake; a later explicit hello need not repeat that handshake field.
    load_balanced = client.options.load_balanced is True
    if (
        hello.get("ok") != 1
        or not isinstance(wire, int)
        or isinstance(wire, bool)
        or wire < 9
        or not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or timeout <= 0
        or not (replica_primary or mongos or load_balanced)
    ):
        raise DatabaseTopologyError(
            "MongoDB transactions require a supported writable replica set "
            "or sharded cluster with sessions"
        )


async def connect_database(settings: Settings) -> tuple[AsyncMongoClient, Any]:
    if not settings.mongo_uri.get_secret_value():
        raise RuntimeError("SUMMON_MONGO_URI is required to connect to MongoDB")
    client: AsyncMongoClient = AsyncMongoClient(
        settings.mongo_uri.get_secret_value(),
        tz_aware=True,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        maxPoolSize=settings.mongo_max_pool_size,
        retryWrites=True,
    )
    try:
        await client.admin.command("ping")
        await require_transaction_topology(client)
        database = client[settings.mongo_database]
        await initialize_database(database, include_poller_health=settings.poller_heartbeat_enabled)
        return client, database
    except BaseException:
        await client.close()
        raise
