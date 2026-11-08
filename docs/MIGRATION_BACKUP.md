# Migration, backup and recovery

**No production data has been read, migrated, backed up, restored, renamed or deleted in this session.** The tools and repositories inspected were references only. Do not import user data, tracked session files or logs without explicit authorization from the responsible operator.

## Safe migration plan — no importer implemented

1. Inventory the real source schema, versions, counts, unique IDs, rarities, media, group settings and duplicate quantities. Define ownership-preserving mappings for both the reference Mongo collections (including `anime_characters_lol` and `user_collection_lmaoooo`) and any authorized SQLite/PostgreSQL source. Flag ambiguous IDs, unsupported rarities and missing artwork rather than silently discarding them.
2. Create an encrypted, access-controlled source backup and verify a restore in an isolated environment. Record source checkpoints and establish a rollback owner. Do not send URIs or backup files through Telegram.
3. Use the offline preview below to inspect authorized snapshots and explicit ID/rarity mappings; independently review its limits, reconcile ownership and investigate every ambiguity. A complete dry-run importer, target catalogue validation and proposed-index review still need implementation. Obtain explicit operator approval for the specific source and new target.
4. Stop source writers for cutover. Import into a new isolated MongoDB replica-set database, never overwrite the source, and do not dual-write legacy and SUMMON databases. Keep all Telegram ownership IDs and character quantities intact. Run idempotence, collision, transactional and application-level validation.
5. Reconcile owner-by-owner and character-by-character counts, sample records and totals; rehearse rollback to the untouched source. Cut over only after review. Keep the source immutable and backups according to retention policy. An eventual rollback must avoid losing post-cutover writes; plan a controlled stop and reconciliation, not a blind database swap.

There is currently **no importer or database-writing migration tool** in this repository. Sample seeding is not migration.

## Offline ownership preview and optional snapshot comparison

`summon/migration_preview.py` reads explicitly supplied **local files only**. It does not connect to MongoDB, create reports, modify source/target files, initialize schemas, or import anything. Its assumptions are based on the audited upstream source at `d2c3bce`, not on a verified production database:

- Legacy catalogue JSONL: one document per line from `anime_characters_lol`; the checked fields are `id` (a string), `name`, `anime`, `rarity` and `img_url`.
- Legacy owners JSONL: one document per line from `user_collection_lmaoooo`; `id` is a positive integer Telegram ID and `characters` is an array of objects with string `id`. Each array entry is one **observed** copy. The tool never treats `"01"` as `"1"`, coerces a numeric source ID, silently resolves duplicate owner documents, or infers mappings from names. It flags duplicate owner documents and may aggregate their observed copies for diagnostics only; it excludes them from mapped totals and blocks target comparison.
- Optional target inventory JSONL: one document per line from a separately authorized SUMMON `inventory` snapshot, with numeric `user_id`, string `character_id`, and non-negative integer `quantity`. Zero-quantity rows do not constitute ownership. The target snapshot is optional and is never contacted live.
- JSONL accepts standard JSON and MongoDB Extended JSON (relaxed or canonical), including BSON 64-bit integer and ObjectId encodings. Each line must be one document, not a JSON array. Rejecting a document, unsupported shape, absent mandatory field or an invalid ID requires investigation; do not assume the omitted data is empty.

The mapping manifest is a separate UTF-8 JSON object. It must list exact legacy character IDs and legacy rarity text explicitly; this synthetic example is **not** a mapping for real data:

```json
{
  "version": 1,
  "character_ids": {"01": "legacy-01", "02": "legacy-02"},
  "rarities": {"Legacy rare": "rare"}
}
```

Target character IDs must match `[a-z0-9][a-z0-9-]{0,63}` and map one-to-one. Target rarities must be `common`, `rare`, `epic` or `legendary`. Duplicate JSON keys are rejected. The preview flags duplicate catalogue/owner records, invalid or unmapped IDs, collisions, unknown owned characters, stale embedded metadata, invalid artwork and unmapped rarities. Counts may overlap and do not prove that fields or records missing from an export never existed. Favorites and other profile fields are only counted as present, **not** migrated or reconciled.

With operator-authorized, consistent exports and an independently reviewed manifest, run locally:

```bash
python -m summon.migration_preview \
  --catalog /secure/exports/catalog.jsonl \
  --owners /secure/exports/owners.jsonl \
  --mapping /secure/exports/mapping.json
# Optionally add: --target-inventory /secure/exports/target-inventory.jsonl
```

The JSON report goes to stdout. By default it contains aggregate counts and static issue names, not user IDs, character IDs, names, usernames, artwork URLs or source paths. Aggregate information is still sensitive. `--include-private-details` explicitly adds per-owner/per-character identities, quantities and optional comparison results; owner IDs and comparison quantities are decimal **strings** to avoid JavaScript precision loss. This private output can identify users: do not send it to Telegram, chat, shared logs or CI; protect any operator-created report with access controls and encryption, and keep exports/reports outside the repository. Error messages do not intentionally echo input paths or contents.

The optional comparison computes matching, missing, unexpected and different-quantity owner/character pairs and copy totals against the two supplied snapshots. Both exports must be complete and taken at the intended consistent checkpoints; a filtered, stale or incomplete export can give misleading results and cannot be detected from these files alone. The tool refuses to compare if the recognized source ownership or target rows are ambiguous or malformed; corresponding result counts are `null`, not zero. A match says nothing about target catalogue identity, source/target snapshot consistency, target indexes, permissions, fields outside this subset or data created after the snapshots. Review the private per-pair report only in a protected environment.

Exit status `0` means no issues **detected in this limited preview**, not that migration is approved; `1` means detected issues or comparison differences; `2` means unreadable, invalid or oversized inputs, with no report. The output always says `safe_to_import: false`, `importer_available: false`, and `source_snapshot_verified: false`. It refuses symlinks, pipes and other non-regular input files, and checks for basic file changes while each file is read; these checks cannot establish snapshot consistency. Limits are 200,000 catalogue documents/mappings, 1,000,000 owner documents, 2,000,000 observed embedded copies, 2,000,000 target inventory documents, 10,000 rarity mappings, 2 GiB per JSONL file and 32 MiB per JSONL record or mapping file. Aggregation is in memory; use an isolated host with sufficient resources. Larger or differently shaped exports need separately reviewed tooling, not silent truncation.

The automated tests use synthetic records and do not read user data or prove real deployment behavior. Full source-schema review, migration mapping, dry-run import, target-side catalogue/metadata reconciliation, real transactions, restore, cutover and rollback remain release blockers.

## Backup and restore controls

Use managed point-in-time recovery or authorized `mongodump`/`mongorestore` operations with credentials delivered by the operator's secure configuration/secret mechanism. Restrict, encrypt and periodically verify backups; never print connection strings or credentials. Record who may access backups, retention, region, restore time objective and audit events.

A restore must target a new isolated database or cluster first. Before any destructive restore or cutover, obtain the authorized operator's explicit confirmation of the destination, time range, backup identity and impact; verify it is not a live/source database. Reconcile schemas, totals, inventory ownership and application checks before switching traffic. Do not build or use a Telegram command that accepts arbitrary source/destination URIs or clears collections.

The reference `/mongobackup` was found to accept and echo connection strings and clear destination data without evident authorization. If the reference is deployed, its owner should disable it immediately and rotate exposed credentials. This repository does not include that command, and this session did not modify the upstream deployment.
