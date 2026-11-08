"""Encrypted backup format, tamper handling and restore safety without live services."""

import stat
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId

from summon.backup import (
    BACKUP_COLLECTIONS,
    BackupError,
    _read_encrypted,
    _records,
    export_snapshot,
    inspect_backup,
    restore_empty,
    scrub_secrets,
)

PASSPHRASE = "a sufficiently long test passphrase"


class SnapshotSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class Cursor:
    def __init__(self, documents, *, fail=False):
        self.documents = documents
        self.fail = fail

    def sort(self, *_args):
        return self

    def __aiter__(self):
        async def generate():
            for document in self.documents:
                yield document
            if self.fail:
                raise RuntimeError("snapshot expired")

        return generate()


class SnapshotDB:
    name = "summon_source"

    def __init__(self, documents=None, *, fail=False):
        self.documents = documents or {}
        self.fail = fail

    def __getitem__(self, name):
        return SimpleNamespace(
            find=lambda *_args, **_kwargs: Cursor(self.documents.get(name, []), fail=self.fail)
        )


class SnapshotClient:
    def start_session(self, *, snapshot):
        assert snapshot is True
        return SnapshotSession()


async def test_encrypted_snapshot_roundtrips_bson_and_excludes_secret_fields(tmp_path):
    object_id = ObjectId()
    db = SnapshotDB(
        {
            "users": [
                {
                    "_id": 42,
                    "name": "PRIVATE PLAYER",
                    "bot_token": "SECRET VALUE",
                    "nested": {"session_string": "another secret", "safe": 3},
                }
            ],
            "inventory": [
                {
                    "_id": object_id,
                    "user_id": 42,
                    "quantity": 2,
                    "character_id": "aiko",
                    "acquired_at": datetime.now(UTC),
                }
            ],
            "character_artworks": [{"_id": "digest", "content": b"legacy-artwork"}],
            "web_sessions": [{"_id": "session-value"}],
        }
    )
    path = tmp_path / "snapshot.sbak"
    counts = await export_snapshot(SnapshotClient(), db, path, PASSPHRASE)
    assert counts["users"] == counts["inventory"] == counts["character_artworks"] == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    encrypted = path.read_bytes()
    for private in (b"PRIVATE PLAYER", b"SECRET VALUE", b"legacy-artwork", b"session-value"):
        assert private not in encrypted
    header, verified = inspect_backup(encrypted, PASSPHRASE)
    assert header["source_database"] == "summon_source" and verified == counts
    records = list(_records(encrypted, PASSPHRASE))
    docs = {row["collection"]: row["document"] for row in records if row["type"] == "document"}
    assert docs["users"] == {"_id": 42, "name": "PRIVATE PLAYER", "nested": {"safe": 3}}
    assert docs["inventory"]["_id"] == object_id
    assert docs["character_artworks"]["content"] == b"legacy-artwork"
    assert "web_sessions" not in docs
    assert set(counts) == set(BACKUP_COLLECTIONS)


async def test_tampering_wrong_password_and_truncation_never_verify(tmp_path):
    path = tmp_path / "snapshot.sbak"
    await export_snapshot(SnapshotClient(), SnapshotDB(), path, PASSPHRASE)
    encrypted = path.read_bytes()
    tampered = bytearray(encrypted)
    tampered[len(tampered) // 2] ^= 1
    for content, password in (
        (bytes(tampered), PASSPHRASE),
        (encrypted[:-1], PASSPHRASE),
        (encrypted, "wrong but sufficiently long passphrase"),
    ):
        with pytest.raises(BackupError):
            inspect_backup(content, password)


async def test_export_failure_removes_only_its_own_incomplete_file(tmp_path):
    path = tmp_path / "incomplete.sbak"
    with pytest.raises(RuntimeError, match="snapshot expired"):
        await export_snapshot(SnapshotClient(), SnapshotDB(fail=True), path, PASSPHRASE)
    assert not path.exists()
    path.write_bytes(b"existing")
    with pytest.raises(BackupError):
        await export_snapshot(SnapshotClient(), SnapshotDB(), path, PASSPHRASE)
    assert path.read_bytes() == b"existing"


async def test_restore_rejects_nonempty_or_unsafe_target_before_writes(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.sbak"
    await export_snapshot(SnapshotClient(), SnapshotDB(), path, PASSPHRASE)
    encrypted = _read_encrypted(path)
    nonempty = SimpleNamespace(
        list_collection_names=AsyncMock(return_value=["users"]),
    )

    class NonemptyTarget:
        list_collection_names = nonempty.list_collection_names

        def __getitem__(self, _name):
            return SimpleNamespace(count_documents=AsyncMock(return_value=1))

    class Client:
        def __getitem__(self, _name):
            return NonemptyTarget()

    initialize = AsyncMock()
    monkeypatch.setattr("summon.backup.initialize_database", initialize)
    with pytest.raises(BackupError):
        await restore_empty(Client(), encrypted, PASSPHRASE, "summon_source")
    with pytest.raises(BackupError):
        await restore_empty(Client(), encrypted, PASSPHRASE, "summon_restore_isolated")
    initialize.assert_not_awaited()


def test_scrubbing_is_recursive_and_does_not_mutate_source():
    source = {"api_hash": "secret", "items": [{"session": "secret", "name": "safe"}]}
    assert scrub_secrets(source) == {"items": [{"name": "safe"}]}
    assert source["api_hash"] == "secret"


def test_backup_cli_requires_explicit_confirmation_and_never_prints_passphrase(monkeypatch, capsys):
    import sys

    from summon.backup import main

    monkeypatch.setenv("SUMMON_BACKUP_PASSPHRASE", PASSPHRASE)
    monkeypatch.setattr(
        sys,
        "argv",
        ["backup.py", "restore", "backup.sbak", "--target-database", "summon_restore_a"],
    )
    with pytest.raises(SystemExit):
        main()
    assert PASSPHRASE not in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", ["backup.py", "export", "backup.sbak"])
    with pytest.raises(SystemExit):
        main()
    assert PASSPHRASE not in capsys.readouterr().err
