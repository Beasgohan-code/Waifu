"""Offline, read-only ownership preview for the audited legacy MongoDB shape.

This is not an importer and cannot verify a snapshot, target state, or cutover.
It accepts local exports and explicit mappings only; it never connects to MongoDB.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, NoReturn, TypeGuard

from bson.errors import BSONError
from bson.json_util import object_hook
from pydantic import ValidationError

from summon.models import CharacterInput
from summon.rarities import RARITY_VALUES

MAX_CATALOG_DOCUMENTS = 200_000
MAX_OWNER_DOCUMENTS = 1_000_000
MAX_TARGET_DOCUMENTS = 2_000_000
MAX_OWNED_ENTRIES = 2_000_000
MAX_RARITY_MAPPINGS = 10_000
MAX_JSONL_LINE_BYTES = 32 * 1024 * 1024
MAX_JSONL_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MAPPING_BYTES = 32 * 1024 * 1024
MAX_BSON_INT64 = (1 << 63) - 1
_TARGET_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_RARITIES = frozenset(RARITY_VALUES)

# Static keys prevent source field names, PII, or arbitrary error text from
# leaking into the default report. Counts can overlap; none imply import safety.
_ISSUE_KEYS = (
    "empty_catalog",
    "empty_owners",
    "invalid_catalog_documents",
    "invalid_catalog_ids",
    "duplicate_catalog_ids",
    "invalid_name_or_category_documents",
    "invalid_artwork_documents",
    "unmapped_rarity_documents",
    "invalid_owner_documents",
    "invalid_owner_ids",
    "duplicate_owner_ids",
    "missing_or_invalid_character_lists",
    "unassigned_owner_entries",
    "invalid_owned_entries",
    "unknown_character_copies",
    "ambiguous_catalog_character_copies",
    "unmapped_character_copies",
    "invalid_target_character_copies",
    "colliding_character_copies",
    "embedded_metadata_mismatch_copies",
    "invalid_source_mappings",
    "invalid_target_mappings",
    "colliding_target_ids",
    "unmapped_catalog_ids",
    "unused_character_mappings",
    "invalid_rarity_mappings",
    "unused_rarity_mappings",
)

# A comparison is suppressed when these issues make the source ownership
# projection incomplete or ambiguous. Metadata warnings remain separate.
_OWNERSHIP_BLOCKING_ISSUES = frozenset(
    {
        "empty_catalog",
        "empty_owners",
        "invalid_catalog_documents",
        "invalid_catalog_ids",
        "duplicate_catalog_ids",
        "invalid_owner_documents",
        "invalid_owner_ids",
        "duplicate_owner_ids",
        "missing_or_invalid_character_lists",
        "unassigned_owner_entries",
        "invalid_owned_entries",
        "unknown_character_copies",
        "ambiguous_catalog_character_copies",
        "unmapped_character_copies",
        "invalid_target_character_copies",
        "colliding_character_copies",
        "invalid_source_mappings",
        "invalid_target_mappings",
        "colliding_target_ids",
        "unmapped_catalog_ids",
        "unused_character_mappings",
    }
)


class PreviewInputError(ValueError):
    """The preview cannot safely interpret the supplied local input."""


def _source_id(value: Any) -> TypeGuard[str]:
    # Never coerce integers or trim: "01" and "1" may be different identities.
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value == value.strip()
        and value.isprintable()
    )


def _target_id(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and _TARGET_ID.fullmatch(value) is not None


def _owner_id(value: Any) -> TypeGuard[int]:
    # bson.int64.Int64 is an int subclass; bool and numeric strings are not IDs.
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_BSON_INT64


def _quantity(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_BSON_INT64


def _mapping_data(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"version", "character_ids", "rarities"}:
        raise PreviewInputError("Invalid mapping manifest")
    if type(value["version"]) is not int or value["version"] != 1:
        raise PreviewInputError("Invalid mapping version")
    characters, rarities = value["character_ids"], value["rarities"]
    if not isinstance(characters, dict) or not isinstance(rarities, dict):
        raise PreviewInputError("Invalid mapping manifest")
    if (
        len(characters) > MAX_CATALOG_DOCUMENTS
        or len(rarities) > MAX_RARITY_MAPPINGS
        or any(not isinstance(key, str) for key in characters)
        or any(not isinstance(key, str) for key in rarities)
    ):
        raise PreviewInputError("Invalid or oversized mapping manifest")
    return characters, rarities


def _catalog_fields(record: dict[str, Any]) -> tuple[bool, bool]:
    """Check relevant target-model constraints without emitting or changing data."""
    try:
        CharacterInput.model_validate(
            {
                "id": "preview",
                "name": record.get("name"),
                "category": record.get("anime"),
                "rarity": "common",
                "image_url": record.get("img_url"),
            }
        )
    except ValidationError as exc:
        locations = {error["loc"][0] for error in exc.errors(include_input=False)}
        return bool(locations & {"name", "category"}), "image_url" in locations
    return False, False


def _compare_target(
    target: Iterable[Any],
    owner_characters: dict[int, Counter[str]],
    character_mapping: dict[str, str],
    source_issues: dict[str, int],
    *,
    include_private_details: bool,
) -> tuple[dict[str, Any], list[dict[str, str]] | None]:
    """Compare two supplied snapshots only when their ownership shapes are unambiguous."""
    target_issues = dict.fromkeys(
        (
            "invalid_target_documents",
            "invalid_target_user_ids",
            "invalid_target_character_ids",
            "invalid_target_quantities",
            "duplicate_target_pairs",
        ),
        0,
    )
    rows_by_pair: Counter[tuple[int, str]] = Counter()
    target_quantities: dict[tuple[int, str], int] = {}
    target_documents = valid_documents = zero_documents = observed_copies = 0
    for raw in target:
        target_documents += 1
        if target_documents > MAX_TARGET_DOCUMENTS:
            raise PreviewInputError("Too many target inventory documents")
        if not isinstance(raw, dict):
            target_issues["invalid_target_documents"] += 1
            continue
        user_id, character_id, quantity = (
            raw.get("user_id"),
            raw.get("character_id"),
            raw.get("quantity"),
        )
        valid_user = _owner_id(user_id)
        valid_character = _target_id(character_id)
        valid_quantity = _quantity(quantity)
        target_issues["invalid_target_user_ids"] += int(not valid_user)
        target_issues["invalid_target_character_ids"] += int(not valid_character)
        target_issues["invalid_target_quantities"] += int(not valid_quantity)
        if not (_owner_id(user_id) and _target_id(character_id) and _quantity(quantity)):
            continue
        valid_documents += 1
        observed_copies += quantity
        zero_documents += int(quantity == 0)
        pair = (user_id, character_id)
        rows_by_pair[pair] += 1
        target_quantities.setdefault(pair, quantity)
    target_issues["duplicate_target_pairs"] = sum(count > 1 for count in rows_by_pair.values())

    source_blocked = any(source_issues[key] for key in _OWNERSHIP_BLOCKING_ISSUES)
    target_blocked = any(target_issues.values())
    performed = not (source_blocked or target_blocked)
    if source_blocked and target_blocked:
        reason = "source_and_target_issues"
    elif source_blocked:
        reason = "source_issues"
    elif target_blocked:
        reason = "target_issues"
    else:
        reason = None

    counts: dict[str, Any] = {
        "target_documents": target_documents,
        "valid_target_documents": valid_documents,
        "target_pairs": len(target_quantities),
        "zero_quantity_documents": zero_documents,
        # Quantities can exceed JavaScript's exact integer range.
        "observed_target_copies": str(observed_copies),
        "matched_pairs": None,
        "missing_target_pairs": None,
        "quantity_mismatch_pairs": None,
        "unexpected_target_pairs": None,
        "missing_copies": None,
        "excess_copies": None,
    }
    details: list[dict[str, str]] | None = None
    if performed:
        expected = {
            (user_id, character_mapping[source_id]): quantity
            for user_id, characters in owner_characters.items()
            for source_id, quantity in characters.items()
        }
        matched = missing = mismatched = unexpected = missing_copies = excess_copies = 0
        if include_private_details:
            details = []
        relevant_pairs = set(expected) | {
            pair for pair, quantity in target_quantities.items() if quantity > 0
        }
        for user_id, character_id in sorted(relevant_pairs):
            pair = (user_id, character_id)
            wanted, found = expected.get(pair, 0), target_quantities.get(pair, 0)
            if wanted == found:
                status = "matched"
                matched += 1
            elif wanted and not found:
                status = "missing"
                missing += 1
            elif wanted:
                status = "quantity_mismatch"
                mismatched += 1
            else:
                status = "unexpected"
                unexpected += 1
            missing_copies += max(0, wanted - found)
            excess_copies += max(0, found - wanted)
            if details is not None:
                details.append(
                    {
                        "user_id": str(user_id),
                        "character_id": character_id,
                        "expected_quantity": str(wanted),
                        "target_quantity": str(found),
                        "status": status,
                    }
                )
        counts.update(
            matched_pairs=matched,
            missing_target_pairs=missing,
            quantity_mismatch_pairs=mismatched,
            unexpected_target_pairs=unexpected,
            missing_copies=str(missing_copies),
            excess_copies=str(excess_copies),
        )
    return {
        "performed": performed,
        "reason": reason,
        "counts": counts,
        "issues": target_issues,
    }, details


def build_report(
    catalog: Iterable[Any],
    owners: Iterable[Any],
    mapping: Any,
    *,
    target_inventory: Iterable[Any] | None = None,
    include_private_details: bool = False,
) -> dict[str, Any]:
    """Inspect the known legacy fields; never make changes or infer missing mappings.

    Each embedded ``characters`` element represents one observed copy. Exact
    legacy string IDs and positive integer Telegram IDs are retained; duplicates,
    malformed rows, target collisions and missing references are reported.
    """
    character_mapping, rarity_mapping = _mapping_data(mapping)
    issues = dict.fromkeys(_ISSUE_KEYS, 0)

    valid_character_mapping: dict[str, str] = {}
    for source, target in character_mapping.items():
        if not _source_id(source):
            issues["invalid_source_mappings"] += 1
        elif not _target_id(target):
            issues["invalid_target_mappings"] += 1
        else:
            valid_character_mapping[source] = target
    target_counts = Counter(valid_character_mapping.values())
    collisions = {target for target, count in target_counts.items() if count > 1}
    issues["colliding_target_ids"] = len(collisions)

    valid_rarity_mapping: dict[str, str] = {}
    for source, target in rarity_mapping.items():
        if (
            not source
            or len(source) > 128
            or not source.isprintable()
            or not isinstance(target, str)
            or target not in _RARITIES
        ):
            issues["invalid_rarity_mappings"] += 1
        else:
            valid_rarity_mapping[source] = target

    catalog_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_rarities: set[str] = set()
    catalog_documents = 0
    for raw in catalog:
        catalog_documents += 1
        if catalog_documents > MAX_CATALOG_DOCUMENTS:
            raise PreviewInputError("Too many catalog documents")
        if not isinstance(raw, dict):
            issues["invalid_catalog_documents"] += 1
            continue
        bad_names, bad_artwork = _catalog_fields(raw)
        issues["invalid_name_or_category_documents"] += int(bad_names)
        issues["invalid_artwork_documents"] += int(bad_artwork)
        rarity = raw.get("rarity")
        if isinstance(rarity, str):
            seen_rarities.add(rarity)
        if not isinstance(rarity, str) or rarity not in valid_rarity_mapping:
            issues["unmapped_rarity_documents"] += 1
        source_id = raw.get("id")
        if not _source_id(source_id):
            issues["invalid_catalog_ids"] += 1
        else:
            catalog_by_id[source_id].append(raw)

    issues["empty_catalog"] = int(catalog_documents == 0)
    issues["duplicate_catalog_ids"] = sum(len(rows) > 1 for rows in catalog_by_id.values())
    issues["unmapped_catalog_ids"] = sum(key not in character_mapping for key in catalog_by_id)
    issues["unused_character_mappings"] = sum(
        _source_id(key) and key not in catalog_by_id for key in character_mapping
    )
    issues["unused_rarity_mappings"] = sum(key not in seen_rarities for key in rarity_mapping)

    def character_status(source: str) -> str:
        if source not in catalog_by_id:
            return "unknown_catalog_id"
        if len(catalog_by_id[source]) != 1:
            return "ambiguous_catalog_id"
        if source not in character_mapping:
            return "unmapped"
        if source not in valid_character_mapping:
            return "invalid_target"
        if valid_character_mapping[source] in collisions:
            return "colliding_target"
        return "mapped"

    owner_documents = 0
    owner_doc_counts: Counter[int] = Counter()
    owner_characters: dict[int, Counter[str]] = defaultdict(Counter)
    invalid_entries_by_owner: Counter[int] = Counter()
    observed_owned_entries = 0
    valid_owner_character_entries = 0
    owner_docs_with_favorites = 0
    owner_docs_with_other_fields = 0

    for raw in owners:
        owner_documents += 1
        if owner_documents > MAX_OWNER_DOCUMENTS:
            raise PreviewInputError("Too many owner documents")
        if not isinstance(raw, dict):
            issues["invalid_owner_documents"] += 1
            continue
        owner_docs_with_favorites += int("favorites" in raw)
        owner_docs_with_other_fields += int(bool(set(raw) - {"_id", "id", "characters"}))
        user_id = raw.get("id")
        if _owner_id(user_id):
            owner_doc_counts[user_id] += 1
            # Record valid owners even when their array is empty or malformed.
            owner_characters[user_id]
        else:
            issues["invalid_owner_ids"] += 1
        entries = raw.get("characters")
        if not isinstance(entries, list):
            issues["missing_or_invalid_character_lists"] += 1
            continue
        observed_owned_entries += len(entries)
        if observed_owned_entries > MAX_OWNED_ENTRIES:
            raise PreviewInputError("Too many ownership entries")
        if not _owner_id(user_id):
            issues["unassigned_owner_entries"] += len(entries)
            continue
        for entry in entries:
            source_id = entry.get("id") if isinstance(entry, dict) else None
            if not _source_id(source_id):
                issues["invalid_owned_entries"] += 1
                invalid_entries_by_owner[user_id] += 1
                continue
            valid_owner_character_entries += 1
            owner_characters[user_id][source_id] += 1
            docs = catalog_by_id.get(source_id, [])
            if len(docs) == 1 and any(
                field in entry and entry[field] != docs[0].get(field)
                for field in ("name", "anime", "rarity", "img_url")
            ):
                issues["embedded_metadata_mismatch_copies"] += 1

    issues["empty_owners"] = int(owner_documents == 0)
    duplicate_owners = {user_id for user_id, count in owner_doc_counts.items() if count > 1}
    issues["duplicate_owner_ids"] = len(duplicate_owners)
    owner_character_pairs = sum(len(characters) for characters in owner_characters.values())
    duplicate_copies = sum(
        max(0, quantity - 1)
        for characters in owner_characters.values()
        for quantity in characters.values()
    )

    status_issues = {
        "unknown_catalog_id": "unknown_character_copies",
        "ambiguous_catalog_id": "ambiguous_catalog_character_copies",
        "unmapped": "unmapped_character_copies",
        "invalid_target": "invalid_target_character_copies",
        "colliding_target": "colliding_character_copies",
    }
    mapped_owned_entries = 0
    private_owners = []
    for user_id in sorted(owner_characters):
        character_details = []
        for source_id, quantity in sorted(owner_characters[user_id].items()):
            status = character_status(source_id)
            if status in status_issues:
                issues[status_issues[status]] += quantity
            elif user_id not in duplicate_owners:
                mapped_owned_entries += quantity
            if include_private_details:
                character_details.append(
                    {
                        "source_id": source_id,
                        "target_id": valid_character_mapping.get(source_id),
                        "quantity": quantity,
                        "status": status,
                    }
                )
        if include_private_details:
            private_owners.append(
                {
                    # JSON consumers (especially JavaScript) can round large integers.
                    "user_id": str(user_id),
                    "duplicate_documents": user_id in duplicate_owners,
                    "invalid_entries": invalid_entries_by_owner[user_id],
                    "characters": character_details,
                }
            )

    report: dict[str, Any] = {
        "format": "summon-legacy-ownership-preview-v1",
        "scope": "offline-preview-only",
        "importer_available": False,
        "safe_to_import": False,
        "source_snapshot_verified": False,
        "counts": {
            "catalog_documents": catalog_documents,
            "catalog_ids": len(catalog_by_id),
            "owner_documents": owner_documents,
            "owner_ids": len(owner_doc_counts),
            "observed_owned_entries": observed_owned_entries,
            "valid_owner_character_entries": valid_owner_character_entries,
            "mapped_owned_entries": mapped_owned_entries,
            "owner_character_pairs": owner_character_pairs,
            "observed_duplicate_copies": duplicate_copies,
            "owner_documents_with_favorites": owner_docs_with_favorites,
            "owner_documents_with_other_fields": owner_docs_with_other_fields,
        },
        "issues": issues,
        "has_issues": any(issues.values()),
        "target_comparison": None,
        "unreviewed_data": [
            "Snapshot consistency, omitted fields and documents, and source completeness",
            "Target database, existing ownership, target ID collisions and indexes",
            "Favorites, wishlists, balances, progress, profiles and reward histories",
            "Groups, settings, moderation, audit, trades and other legacy collections",
            "Artwork licensing, availability and actual target rendering",
        ],
    }
    comparison_details = None
    if target_inventory is not None:
        comparison, comparison_details = _compare_target(
            target_inventory,
            owner_characters,
            valid_character_mapping,
            issues,
            include_private_details=include_private_details,
        )
        report["target_comparison"] = comparison
        comparison_counts = comparison["counts"]
        differences = comparison["performed"] and any(
            comparison_counts[key]
            for key in (
                "missing_target_pairs",
                "quantity_mismatch_pairs",
                "unexpected_target_pairs",
            )
        )
        report["has_issues"] = bool(
            report["has_issues"] or any(comparison["issues"].values()) or differences
        )
    if include_private_details:
        report["details"] = {
            "owners": private_owners,
            "catalog": [
                {
                    "source_id": source_id,
                    "target_id": valid_character_mapping.get(source_id),
                    "catalog_documents": len(catalog_by_id[source_id]),
                    "status": character_status(source_id),
                }
                for source_id in sorted(catalog_by_id)
            ],
        }
        if comparison_details is not None:
            report["details"]["comparison"] = comparison_details
    return report


def _parse_json(data: bytes, *, extended: bool) -> Any:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> Any:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PreviewInputError("Duplicate JSON key")
            result[key] = value
        return object_hook(result) if extended else result

    def reject_constant(_value: str) -> NoReturn:
        raise PreviewInputError("Non-finite JSON number")

    try:
        return json.loads(
            data.decode("utf-8"), object_pairs_hook=pairs_hook, parse_constant=reject_constant
        )
    except (
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        RecursionError,
        BSONError,
        AttributeError,
    ) as exc:
        raise PreviewInputError("Invalid JSON input") from exc


@contextmanager
def _open_regular_file(path: Path) -> Iterator[BinaryIO]:
    """Read a stable regular file, refusing symlinks, devices and pipes."""
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise PreviewInputError("Inputs must be regular files")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1  # The file object now owns the descriptor.
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise PreviewInputError("Input changed while opening")
            yield source
            after = os.fstat(source.fileno())
            if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise PreviewInputError("Input changed while reading")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _jsonl_records(path: Path) -> Iterator[Any]:
    with _open_regular_file(path) as source:
        if os.fstat(source.fileno()).st_size > MAX_JSONL_FILE_BYTES:
            raise PreviewInputError("JSONL file exceeds the local limit")
        consumed = 0
        while line := source.readline(MAX_JSONL_LINE_BYTES + 1):
            consumed += len(line)
            if len(line) > MAX_JSONL_LINE_BYTES or consumed > MAX_JSONL_FILE_BYTES:
                raise PreviewInputError("JSONL input exceeds the local limit")
            if line.strip():
                yield _parse_json(line, extended=True)


def _read_mapping(path: Path) -> Any:
    with _open_regular_file(path) as source:
        data = source.read(MAX_MAPPING_BYTES + 1)
    if len(data) > MAX_MAPPING_BYTES:
        raise PreviewInputError("Mapping exceeds the local limit")
    return _parse_json(data, extended=False)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse normally includes rejected arguments, which may contain secrets.
        self.exit(2, "Invalid arguments. Run with --help for usage.\n")


def main(argv: list[str] | None = None) -> int:
    parser = _SafeArgumentParser(
        description="Preview legacy ownership from local JSONL exports; never connects or imports."
    )
    parser.add_argument("--catalog", required=True, type=Path, help="Local character JSONL export")
    parser.add_argument("--owners", required=True, type=Path, help="Local owner JSONL export")
    parser.add_argument(
        "--mapping", required=True, type=Path, help="Explicit JSON mapping manifest"
    )
    parser.add_argument(
        "--target-inventory",
        type=Path,
        help="Optional local SUMMON inventory JSONL export to compare; never connects",
    )
    parser.add_argument(
        "--include-private-details",
        action="store_true",
        help="Include Telegram and character IDs in output; protect the resulting report",
    )
    args = parser.parse_args(argv)
    try:
        mapping = _read_mapping(args.mapping)
        report = build_report(
            _jsonl_records(args.catalog),
            _jsonl_records(args.owners),
            mapping,
            target_inventory=(
                _jsonl_records(args.target_inventory) if args.target_inventory is not None else None
            ),
            include_private_details=args.include_private_details,
        )
    except (PreviewInputError, OSError):
        # Do not print exception details: paths and source contents can be sensitive.
        print(
            "Preview failed: unable to safely read or interpret the supplied inputs.",
            file=sys.stderr,
        )
        return 2
    if args.include_private_details:
        print(
            "Warning: this report contains private identifiers; protect it accordingly.",
            file=sys.stderr,
        )
    print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    return 1 if report["has_issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
