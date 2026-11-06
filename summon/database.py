"""Async MongoDB lifecycle, schema validation and indexes.

Ownership/economy mutations use multi-document transactions, so production must
use a replica set or sharded cluster. No non-transactional fallback is provided.
"""

from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.errors import CollectionInvalid, OperationFailure

from summon.config import Settings
from summon.group_notes import MAX_NOTE_LENGTH, MAX_NOTES, NOTE_NAME_PATTERN, NOTE_REVISION_PATTERN

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
            "rarity": {"enum": ["common", "rare", "epic", "legendary"]},
            "active": {"bsonType": "bool"},
            "random_key": {"bsonType": "double", "minimum": 0, "maximum": 1},
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


async def initialize_database(database: Any) -> None:
    existing = set(await database.list_collection_names())
    for name, schema in COLLECTION_SCHEMAS.items():
        validator = {"$jsonSchema": schema}
        if name not in existing:
            try:
                await database.create_collection(name, validator=validator)
                continue
            except CollectionInvalid:  # Another instance created it concurrently.
                pass
        await database.command("collMod", name, validator=validator, validationLevel="moderate")
    for name, indexes in INDEXES.items():
        for keys, options in indexes:
            await database[name].create_index(keys, **options)


class DatabaseTopologyError(RuntimeError):
    """The connected deployment cannot be used for transactional writes."""


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
        await initialize_database(database)
        return client, database
    except BaseException:
        await client.close()
        raise
