"""Operator-only, encrypted MongoDB snapshot backups; never sent through Telegram.

Exports preserve BSON types, legacy artwork, catalog numbers, inventory and audit
history. The separately configured Wzgram session database, web sessions, auth
nonces and rate limits are intentionally excluded. Restoration is insert-only and
limited to an explicitly named empty restore database after writers are stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import os
import re
import secrets
import sys
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bson import BSON, json_util
from bson.errors import BSONError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from pymongo import AsyncMongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.write_concern import WriteConcern

from summon.config import BackupConnectionSettings
from summon.database import initialize_database, require_transaction_topology

MAGIC = b"SUMMONBK\x01"
SALT_BYTES = 16
NONCE_BYTES = 12
TAG_BYTES = 16
HEADER_BYTES = len(MAGIC) + SALT_BYTES + NONCE_BYTES
MAX_BACKUP_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_LINE_BYTES = 32 * 1024 * 1024
MAX_DOCUMENTS = 1_000_000
CHUNK_BYTES = 64 * 1024
BACKUP_COLLECTIONS = (
    "characters",
    "character_artworks",
    "catalog_counters",
    "spawn_weights",
    "users",
    "inventory",
    "wishlists",
    "groups",
    "group_admins",
    "warnings",
    "gifts",
    "trades",
    "spawns",
    "activity",
    "reward_history",
)
# An allowlist of collections excludes session/auth databases. Strip known
# credential-shaped field names recursively, but arbitrary user-entered text can
# still contain sensitive information and the encrypted file must be protected.
_SECRET_FIELDS = frozenset(
    {
        "access_hash",
        "access_token",
        "api_hash",
        "api_key",
        "auth_key",
        "authorization",
        "bearer_token",
        "bot_token",
        "catbox_userhash",
        "client_secret",
        "credential",
        "credentials",
        "delete_url",
        "imgbb_key",
        "mongo_uri",
        "mongo_url",
        "password",
        "password_hash",
        "private_key",
        "refresh_token",
        "secret",
        "session",
        "session_id",
        "session_key",
        "session_secret",
        "session_string",
        "token",
        "userhash",
    }
)
_SECRET_FIELD_KEYS = frozenset(re.sub(r"[^a-z0-9]", "", name) for name in _SECRET_FIELDS)


class BackupError(RuntimeError):
    """A backup could not be safely created, verified, or restored."""


def scrub_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: scrub_secrets(item)
            for key, item in value.items()
            if not isinstance(key, str)
            or re.sub(r"[^a-z0-9]", "", key.casefold()) not in _SECRET_FIELD_KEYS
        }
    if isinstance(value, list):
        return [scrub_secrets(item) for item in value]
    return value


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    encoded = passphrase.encode("utf-8") if isinstance(passphrase, str) else b""
    if not 16 <= len(encoded) <= 1024:
        raise BackupError("Use a backup passphrase of 16–1024 UTF-8 bytes")
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(encoded)


def _json_line(value: dict[str, Any]) -> bytes:
    result = (
        json_util.dumps(
            value,
            json_options=json_util.CANONICAL_JSON_OPTIONS,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        + b"\n"
    )
    if len(result) > MAX_LINE_BYTES:
        raise BackupError("A backup record is too large")
    return result


def _open_new(path: Path) -> tuple[int, os.stat_result]:
    if path.suffix != ".sbak":
        raise BackupError("Backup output must use the .sbak extension")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        return fd, os.fstat(fd)
    except OSError:
        raise BackupError(
            "Could not create a new backup file; existing files are never overwritten"
        ) from None


def _remove_own_incomplete(path: Path, created: os.stat_result) -> None:
    try:
        current = path.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
            path.unlink()
    except OSError:
        pass


async def export_snapshot(
    client: Any, database: Any, output: Path, passphrase: str
) -> dict[str, int]:
    """Write one majority snapshot as a streaming gzip + AES-256-GCM file.

    MongoDB 5.0+ snapshot sessions are required. A history expiry or any other
    failure aborts the export; a newer inconsistent snapshot is never substituted.
    """
    salt, nonce = secrets.token_bytes(SALT_BYTES), secrets.token_bytes(NONCE_BYTES)
    key = _derive_key(passphrase, salt)
    header = MAGIC + salt + nonce
    cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(header)
    compressor = zlib.compressobj(level=6, wbits=31)
    fd, created = _open_new(output)
    counts = dict.fromkeys(BACKUP_COLLECTIONS, 0)
    written = 0
    expanded = 0
    complete = False
    try:
        with os.fdopen(fd, "wb") as destination:
            destination.write(header)
            written = len(header)

            def emit(value: dict[str, Any]) -> None:
                nonlocal written, expanded
                line = _json_line(value)
                expanded += len(line)
                if expanded > MAX_EXPANDED_BYTES:
                    raise BackupError("Backup expands beyond the safety limit")
                encrypted = cipher.update(compressor.compress(line))
                written += len(encrypted)
                if written + TAG_BYTES > MAX_BACKUP_BYTES:
                    raise BackupError("Backup exceeds the 256 MiB encrypted-file limit")
                destination.write(encrypted)

            emit(
                {
                    "type": "header",
                    "format": "summon-backup-v1",
                    "source_database": database.name,
                    "created_at": datetime.now(UTC),
                    "collections": list(BACKUP_COLLECTIONS),
                }
            )
            async with client.start_session(snapshot=True) as session:
                for collection in BACKUP_COLLECTIONS:
                    cursor = database[collection].find({}, session=session).sort("_id", 1)
                    async for document in cursor:
                        counts[collection] += 1
                        if sum(counts.values()) > MAX_DOCUMENTS:
                            raise BackupError("Backup exceeds the document limit")
                        emit(
                            {
                                "type": "document",
                                "collection": collection,
                                "document": scrub_secrets(document),
                            }
                        )
            emit({"type": "end", "counts": counts})
            final = cipher.update(compressor.flush()) + cipher.finalize() + cipher.tag
            if written + len(final) > MAX_BACKUP_BYTES:
                raise BackupError("Backup exceeds the 256 MiB encrypted-file limit")
            destination.write(final)
            destination.flush()
            os.fsync(destination.fileno())
        complete = True
        return counts
    finally:
        if not complete:
            _remove_own_incomplete(output, created)


def _read_encrypted(path: Path) -> bytes:
    try:
        with path.open("rb") as source:
            encrypted = source.read(MAX_BACKUP_BYTES + 1)
    except OSError:
        raise BackupError("Could not read the encrypted backup") from None
    if not HEADER_BYTES + TAG_BYTES < len(
        encrypted
    ) <= MAX_BACKUP_BYTES or not encrypted.startswith(MAGIC):
        raise BackupError("Invalid or oversized backup")
    return encrypted


def _decryptor(encrypted: bytes, passphrase: str) -> Any:
    header = encrypted[:HEADER_BYTES]
    salt = header[len(MAGIC) : len(MAGIC) + SALT_BYTES]
    nonce = header[-NONCE_BYTES:]
    decryptor = Cipher(
        algorithms.AES(_derive_key(passphrase, salt)), modes.GCM(nonce, encrypted[-TAG_BYTES:])
    ).decryptor()
    decryptor.authenticate_additional_data(header)
    return decryptor


def _cipher_chunks(encrypted: bytes) -> Iterator[bytes]:
    view = memoryview(encrypted)[HEADER_BYTES:-TAG_BYTES]
    for offset in range(0, len(view), CHUNK_BYTES):
        yield bytes(view[offset : offset + CHUNK_BYTES])


def _authenticate(encrypted: bytes, passphrase: str) -> None:
    decryptor = _decryptor(encrypted, passphrase)
    try:
        for chunk in _cipher_chunks(encrypted):
            decryptor.update(chunk)
        decryptor.finalize()
    except (InvalidTag, ValueError):
        raise BackupError("Backup authentication failed; passphrase or file is incorrect") from None


def _records(encrypted: bytes, passphrase: str) -> Iterator[dict[str, Any]]:
    """Read bounded gzip JSONL after the immutable ciphertext has been authenticated."""
    decryptor = _decryptor(encrypted, passphrase)
    decompressor = zlib.decompressobj(wbits=31)
    pending = bytearray()
    expanded = 0
    try:
        for chunk in _cipher_chunks(encrypted):
            compressed = decryptor.update(chunk)
            while compressed:
                part = decompressor.decompress(compressed, CHUNK_BYTES)
                compressed = decompressor.unconsumed_tail
                expanded += len(part)
                if expanded > MAX_EXPANDED_BYTES:
                    raise BackupError("Backup expands beyond the safety limit")
                pending.extend(part)
                while b"\n" in pending:
                    line, _, remainder = pending.partition(b"\n")
                    pending = bytearray(remainder)
                    if len(line) > MAX_LINE_BYTES:
                        raise BackupError("Backup record is too large")
                    value = json_util.loads(line.decode("utf-8", "strict"))
                    if not isinstance(value, dict):
                        raise BackupError("Invalid backup record")
                    yield value
                if len(pending) > MAX_LINE_BYTES:
                    raise BackupError("Backup record is too large")
        decryptor.finalize()
        if not decompressor.eof or decompressor.unused_data or pending:
            raise BackupError("Incomplete or malformed backup")
    except BackupError:
        raise
    except (BSONError, InvalidTag, ValueError, UnicodeError, zlib.error, TypeError, RecursionError):
        raise BackupError("Backup content is invalid") from None


def inspect_backup(encrypted: bytes, passphrase: str) -> tuple[dict[str, Any], dict[str, int]]:
    """Authenticate and validate the entire archive before any restore writes."""
    if (
        not isinstance(encrypted, bytes)
        or not HEADER_BYTES + TAG_BYTES < len(encrypted) <= MAX_BACKUP_BYTES
        or not encrypted.startswith(MAGIC)
    ):
        raise BackupError("Invalid or oversized backup")
    _authenticate(encrypted, passphrase)
    header: dict[str, Any] | None = None
    expected: dict[str, int] | None = None
    counts = dict.fromkeys(BACKUP_COLLECTIONS, 0)
    identities: set[bytes] = set()
    total = 0
    for record in _records(encrypted, passphrase):
        kind = record.get("type")
        if header is None:
            if (
                kind != "header"
                or record.get("format") != "summon-backup-v1"
                or record.get("collections") != list(BACKUP_COLLECTIONS)
                or not isinstance(record.get("source_database"), str)
            ):
                raise BackupError("Unsupported backup header")
            header = record
            continue
        if expected is not None:
            raise BackupError("Backup contains records after its end marker")
        if kind == "end":
            expected = record.get("counts")
            if (
                not isinstance(expected, dict)
                or set(expected) != set(BACKUP_COLLECTIONS)
                or any(type(value) is not int or value < 0 for value in expected.values())
            ):
                raise BackupError("Invalid backup counts")
            continue
        collection = record.get("collection")
        document = record.get("document")
        if (
            kind != "document"
            or collection not in counts
            or not isinstance(document, dict)
            or "_id" not in document
        ):
            raise BackupError("Invalid backup document")
        if scrub_secrets(document) != document:
            raise BackupError("Backup contains excluded credential fields")
        try:
            encoded = BSON.encode(document)
            identity = hashlib.sha256(
                collection.encode() + b"\0" + BSON.encode({"_id": document["_id"]})
            ).digest()
        except (BSONError, ValueError, TypeError, OverflowError):
            raise BackupError("Invalid BSON in backup") from None
        if len(encoded) > 16 * 1024 * 1024 or identity in identities:
            raise BackupError("Oversized or duplicate backup document")
        identities.add(identity)
        counts[collection] += 1
        total += 1
        if total > MAX_DOCUMENTS:
            raise BackupError("Backup exceeds the document limit")
    if header is None or expected != counts:
        raise BackupError("Backup is incomplete or its counts do not match")
    return header, counts


async def restore_empty(
    client: Any, encrypted: bytes, passphrase: str, target_name: str
) -> dict[str, int]:
    """Insert a fully verified archive into a separately named, empty restore DB.

    The caller must explicitly confirm that all writers are stopped. Partial
    failures are never cleaned up automatically; inspect the isolated target.
    """
    header, counts = inspect_backup(encrypted, passphrase)
    if (
        not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*_restore_[A-Za-z0-9_-]+", target_name)
        or target_name == header["source_database"]
    ):
        raise BackupError("Use a separate database named like summon_restore_example")
    database = client[target_name]
    for name in await database.list_collection_names():
        if name.startswith("system.") or await database[name].count_documents({}, limit=1):
            raise BackupError("Restore target must be entirely empty")
    # This majority-acknowledged, insert-only marker prevents two restores
    # from both proceeding after independently observing an empty target.
    guard = database["summon_restore_guard"].with_options(
        write_concern=WriteConcern(w="majority", wtimeout=5000)
    )
    try:
        await guard.insert_one({"_id": "restore-v1", "created_at": datetime.now(UTC)})
    except DuplicateKeyError:
        raise BackupError("Another restore already claimed this target") from None
    await initialize_database(database)
    for record in _records(encrypted, passphrase):
        if record.get("type") == "document":
            await database[record["collection"]].insert_one(record["document"])
    return counts


def _passphrase() -> str:
    value = os.environ.get("SUMMON_BACKUP_PASSPHRASE")
    if value is None:
        if not sys.stdin.isatty():
            raise BackupError("A terminal or SUMMON_BACKUP_PASSPHRASE is required")
        value = getpass.getpass("Backup passphrase: ")
    return value


async def _run(args: argparse.Namespace, passphrase: str) -> dict[str, int]:
    if args.operation == "verify":
        return inspect_backup(_read_encrypted(args.file), passphrase)[1]
    settings = BackupConnectionSettings()
    uri = settings.mongo_uri.get_secret_value()
    if not uri:
        raise BackupError("MONGO_URL is required")
    client: AsyncMongoClient = AsyncMongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=5000)
    try:
        await client.admin.command("ping")
        await require_transaction_topology(client)
        if args.operation == "export":
            hello = await client.admin.command("hello")
            if hello.get("maxWireVersion", 0) < 13:
                raise BackupError("Encrypted snapshot backup requires MongoDB 5.0 or newer")
            return await export_snapshot(
                client, client[settings.mongo_database], args.file, passphrase
            )
        if args.target_database in {settings.mongo_database, settings.telegram_session_database}:
            raise BackupError(
                "Restore target must differ from the application and session databases"
            )
        return await restore_empty(
            client, _read_encrypted(args.file), passphrase, args.target_database
        )
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    export = subparsers.add_parser("export", help="create a new encrypted snapshot file")
    export.add_argument("file", type=Path)
    export.add_argument("--confirm", required=True, choices=["EXPORT-ENCRYPTED"])
    verify = subparsers.add_parser("verify", help="authenticate and inspect an encrypted file")
    verify.add_argument("file", type=Path)
    restore = subparsers.add_parser("restore", help="insert into a separate empty restore database")
    restore.add_argument("file", type=Path)
    restore.add_argument("--target-database", required=True)
    restore.add_argument("--writers-stopped", action="store_true", required=True)
    restore.add_argument("--confirm", required=True, choices=["RESTORE-INTO-EMPTY"])
    args = parser.parse_args()
    try:
        counts = asyncio.run(_run(args, _passphrase()))
    except BackupError as exc:
        # BackupError messages are static and deliberately omit data and secrets.
        print(f"Backup operation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except (PyMongoError, OSError, TimeoutError, ValueError) as exc:
        # Never print driver exception details: they can contain connection URIs.
        print(
            f"Backup operation failed ({type(exc).__name__}); no automatic cleanup or retry.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(
        f"{args.operation.capitalize()} completed: {sum(counts.values())} documents "
        f"across {len(counts)} collections."
    )


if __name__ == "__main__":
    main()
