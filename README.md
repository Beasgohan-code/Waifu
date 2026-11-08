# SUMMON ✦

**Collect. Discover. Summon.**

SUMMON is an async Telegram character-collection bot, MongoDB-backed group-management service, and mobile-first Mini App. Collection, daily rewards, progression, favorites, search, consent-based trades and gifts, group settings, public notes, and opt-in auto replies are implemented without gambling, deposits, or cash-out.

> **Prototype, not deployment-ready.** This Wzgram migration has not been tested against live Telegram or a production MongoDB deployment. Offline native regression tests and a prior isolated, single-member MongoDB CI run do not verify a real cutover, production permissions, or failover. See [todo.md](todo.md) for current release gates.

## Telegram behavior

The bot registers message, edited-message, and callback handlers **directly with Wzgram**; there is no aiogram dispatcher or bridge. Group protection runs before commands, and ownership-changing operations retain their MongoDB transaction and consent checks. API workers use separate Wzgram sessions with updates disabled, not a second update consumer.

The group worker keeps its Wzgram session, update state, and known peers in a separate MongoDB database. A majority-write lease coordinates workers configured against the same application database and bot ID. It is not fencing for every in-flight operation and does not guarantee exactly-once delivery. Wzgram's bounded dispatcher queue can drop updates during sustained saturation. Recovery is limited by stored MTProto state and Telegram's retention; Bot API updates from before the initial cutover are not automatically migrated.

Private group results attempt Wzgram's ephemeral messages, then a private chat; a public fallback contains only a prompt to open the bot, not the private result. Rich messages and selected premium emoji IDs have readable Unicode fallbacks after definite rejections. Telegram account eligibility, pack permissions, and live ephemeral/rich rendering remain unverified. Normal command-menu entries do not imply that a command itself is private.

**Mute, unmute, unban and kick are intentionally unavailable in SUMMON:** Wzgram restriction writes can clear a ban, and there is no atomic guard against another administrator banning the target between a membership check and the write. Use Telegram's own group controls for these actions. The commands and API action names still return an explicit denial; ban and warning records remain available subject to permissions. Public group notes and auto replies are visible to group members and must never contain secrets.

## Read-only preview

Requires Python 3.11+ and Node.js 22+. From the repository root:

```bash
python3 -m venv .bootstrap
.bootstrap/bin/python -m pip install 'uv==0.13.7'
.bootstrap/bin/uv sync --locked --extra dev --python 3.11 --no-python-downloads
cd web && npm ci && cd ..

# Terminal 1: fictional, read-only API; no Telegram or MongoDB credentials
SUMMON_PREVIEW_MODE=true .venv/bin/uvicorn summon.api.app:app --host 0.0.0.0 --port 8000 --no-access-log

# Terminal 2: browser frontend, with /api proxied to the sample API
cd web && npm run dev
```

The preview rejects mutations and cannot be enabled in production. It is not an end-to-end Telegram or MongoDB test. `uv.lock` pins the Python dependency graph; plain pip installs do not enforce that lock.

## Deployment configuration and cutover

No deployment or cutover has been performed. For an authorized isolated rehearsal, configure `.env.example` values in a secret manager, including the bot token, Telegram application API ID/hash, a random session secret, a MongoDB replica set or sharded cluster, and an HTTPS Mini App URL. Production validation requires the Telegram application credentials on both API and bot processes. Keep all credentials out of frontend bundles, Git, chat, URLs, and logs.

The short, case-insensitive names in `.env.example` include `BOT_TOKEN`, `SESSION_SECRET`, `MONGO_URL`, `MONGO_DB_NAME`, `WEBAPP_URL`, `API_ID`, `API_HASH` (also `API_Hash`), and `OWNER_ID` (one Telegram user ID); `OPERATOR_IDS` accepts a comma-separated list. Existing `SUMMON_*` names and the documented aliases remain supported. Explicit constructor settings take precedence over the process environment, which takes precedence over `.env`; within one source, `SUMMON_*` wins, including an explicitly empty value. Use only one spelling per setting and keep secrets private.

Catalog models recognize the 18 reference rarity tiers while retaining stored `epic` values; legacy Epic and Special Edition share one spawn bucket. Weights start with the reference's initialization defaults, including AMV 10; another reference fallback specifies 100, and a running reference instance can have customized values. Configured operators can use private `/chance rarity-number weight` to change a global spawn weight from 0 to 1,000,000; zero disables that bucket, and zeroing all buckets disables new spawns and free character claims. Changes are transactionally audited, `/chancelist` and `/claimlist` show current values, and in-flight selections may still use the prior weights. Only nonempty active buckets participate.

`/hclaim` and `/claim` award one free character per user per UTC day in a transaction; `/daily` remains a separate points/XP reward. Unlike the reference, this uses the shared spawn weights, has no official-group requirement, premium boost or additional claim, and sends the result privately. New claims also record a Telegram-message replay guard; historical claims made before this update cannot be retroactively identified by message. `/hmode` changes only the sender's collection ordering or favorites filter. In groups, `/spawn` requires a current administrator with change-info permission and uses the same enabled, activity, cooldown, and active-summon gates as automatic spawns; it does not force a summon or count a message. `/groupstats` privately summarizes the last 30 days of recorded group summon reservations and the sender's own catches; it does not expose member identities or rankings. The Mini App shows the same report to currently verified group administrators. An attempt is a reservation, not proof that Telegram delivered it; older or missing records are not counted.

Configured operators can add public media in a private bot chat: reply to a photo, video or GIF with `/upload "Character name" "Anime name" 2`, `/upload Character_Name Anime_Name 2`, or `/upload Character name | Anime name | 2`. The last argument is a rarity number 1–18. Choose Catbox or, for photos when `IMGBB_KEY` is set, ImgBB; review the details and original media before explicitly choosing **Publish publicly** or **Cancel upload**. Selecting a host does not upload, archive or create a character. A confirmed cancellation disables all reviews for that command, but cannot undo an upload already claimed or attempted. Reviews expire 15 minutes after the original command and cannot be used if the command text or source media no longer matches. Videos and GIFs are always marked AMV Edition. `DB_CHANNEL` (or `DB_CHANNEL_ID`) must be a negative channel ID where the bot can post: SUMMON attempts a media copy and saves a Telegram file ID with the public URL. If the copy is unconfirmed, it keeps the original file ID and warns. File IDs are bot/access dependent and are not a durable substitute for independent backups.

Photos must be ordinary JPEGs up to 3 MiB and are transcoded with metadata removed. Videos/GIFs are limited to 16 MiB, must match supported GIF, MP4, WebM or MOV signatures, and can retain embedded metadata. Protected, spoiler and self-destructing media are rejected. New uploads store references, not image/video bytes, in MongoDB; existing artwork blobs remain available and are not deleted. Upload menus expire after 15 minutes. Callbacks are actor-bound and a seven-day replay claim helps avoid duplicate external uploads.

**Artwork is public.** Publish only media you have permission to share. Catbox can restrict anonymous datacenter uploads; an optional secret `CATBOX_USERHASH` may be required and does not guarantee acceptance. ImgBB accepts photos, not the bot's moving-media uploads. Provider uploads and channel copies can be orphaned or unconfirmed if a later step fails; check both before retrying. These providers and channel behavior have not been verified live, and there is no automatic deletion workflow.

`/addchar name | series | rarity | HTTPS-URL` and `/upload id | name | series | rarity | HTTPS-URL` retain URL-only creation. `/updatechar id name|series|rarity|image|description value` (also `/update`) preserves the character key and ownership. For new or updated photos through these older commands, a replied photo requires explicit `ARTWORK_BACKEND=imgbb` and `IMGBB_KEY`; otherwise supply a public HTTPS URL or use the `/upload` menu to create a character. To replace existing artwork through the reviewed media flow, reply to a photo, video or GIF with `/updatechar exact-id media`; `/check` displays the catalog key when it differs from the numeric alias. The review shows the current and proposed rarity, rejects changed records, and leaves previous public files and channel copies untouched. Photos retain the existing rarity; video and GIF replacements become AMV Edition. The default `mongo` backend no longer stores new uploaded photo blobs. Existing old blobs remain readable. The Mini App's operator-only, URL-based catalog editor can load an existing record by its exact internal ID, shows current and proposed details, requires explicit save confirmation and rejects changes made after review. Leaving an existing artwork URL unchanged preserves its Telegram media reference; replacing a motion URL clears that reference and changes the editor's artwork to a photo. The Mini App does not upload media. The older unconditional catalog API remains available for compatibility and can still overwrite newer edits if used directly. Artwork preview is opt-in and contacts the URL's host from the operator's browser. Private `/owner` and `/panel` open the authenticated Mini App, and `/sudolist` lists configured operators read-only.

`/check 01` and `/check 1` refer to numeric character 1. Numeric aliases are additive: existing catalog keys and inventory references remain unchanged. On first numeric lookup or catalog write, a transactional backfill numbers existing records deterministically by creation time and key, preserves compatible existing numbers, and rejects collisions or catalogs above 10,000 records for operator review. Rehearse and inspect the mapping on an authorized isolated copy before rollout; it is not a migration of external data.

Private `/profile` and the Mini App profile share an actor-scoped, read-only snapshot report of owned copies, duplicates, favorites, rarity distribution and active-catalog completion. Retired and missing catalog records still count as owned, but only active owned characters count toward completion; missing or unknown rarity appears as Uncategorized. Collection goals reflect current ownership and can change after a trade or gift; they are not permanent rewards. The best recorded daily streak is preserved on future claims, including the last recorded legacy streak, but earlier streaks that were never stored cannot be reconstructed. Reports require MongoDB 5.0+ snapshot reads and may be temporarily unavailable on slow or unsupported databases; no collection data is changed.

For HTTPS URLs, SUMMON does not fetch or verify the destination: Telegram and app clients fetch the URL, whose DNS or redirects can change. Operators must choose a trusted public host. The public image route rate-limits requests by the observed network host, so shared proxy egress can combine clients. Live Telegram, hosted proxy behavior, and production MongoDB transactions still need an authorized rehearsal.

Run one `summon-bot` worker per bot token and one or more `summon-api` workers. Configure the same bot token, application MongoDB database, separate `SUMMON_TELEGRAM_SESSION_DATABASE`, and session secret consistently. The bot must have read/write/index permissions for its dedicated session database; API workers need read access to its `peers` collection, not the session auth-key collection. That database and its backups contain credential-bearing MTProto session data and sensitive peer metadata. Restrict and encrypt access; never share one MTProto auth session concurrently across workers.

**Before starting the Wzgram worker**, stop all previous consumers and have an authorized operator remove any existing Bot API webhook while preserving pending updates according to the operator's cutover plan. Wzgram cannot inspect or remove that webhook. Only after verifying this, set `SUMMON_WEBHOOKS_DISABLED_CONFIRMED=true` on the bot worker. The confirmation is an operator assertion, not an automatic check. On first cutover, audit any queued Bot API updates separately; do not assume they were delivered to MTProto.

For group operations the API loads known peer access hashes from the worker's cache, then checks current Telegram membership/rights. Unknown or inaccessible peers fail closed rather than granting access; have the affected group/member interact with the bot and retry. Cross-session access hashes, fresh role changes, and API/worker startup and recovery require live verification.

A Render web service can run `summon-api` using `PORT`; a separate worker can run `summon-bot`. The Dockerfile can also serve `web/dist` on the same origin using `SUMMON_WEB_DIST`. On Vercel, set the project root to `web` and set the server-side `SUMMON_BACKEND_ORIGIN` to the Render API's HTTPS origin with no path, query, or credentials. `web/api/[...path].js` forwards same-origin `/api/*` requests and cookies to that origin, with a 64 KiB body limit. Configure Render's `SUMMON_ALLOWED_ORIGINS` to include the exact Vercel HTTPS origin, and set `SUMMON_WEBAPP_URL` to that frontend. The proxy does not trust client-supplied forwarding headers; IP-based sign-in throttles may aggregate users behind shared proxy egress. Vercel routing, Render networking, cookie behavior, and rate limiting require an authorized live rehearsal.

`/health/ready` checks database reachability/topology, not Telegram delivery. When explicitly enabled, the protected `/health/poller` endpoint now reports `recent_connection` or `no_recent_connection` based on a successful MTProto `GetState` RPC; this is **not** a poll, lease, delivery, or handler-health guarantee. Keep its dedicated monitoring token in a secret header only. External alerting and real outage/failover checks remain necessary.

## Migration and recovery safety

No production data, session, log, backup, restore, migration, or credential rotation was performed here. Treat credentials previously committed anywhere as exposed and rotate them through the authorized operator. Never send database URIs or backups through Telegram.

There is no importer. `summon-migration-preview` only analyzes explicitly authorized local JSONL exports and mappings; it does not connect, import, or prove that an export is complete. Protect exports and reports, and do not commit them. Before any actual migration or destructive restore: obtain explicit authorization and confirmation, stop source writers, create encrypted backups, verify an isolated restore, map IDs and quantities without collisions, reconcile ownership and totals, and rehearse rollback. Never overwrite, rename, drop, or dual-write the source as an implicit fallback.

An independently designed, operator-run `backup.py` / `summon-backup` exports an authenticated AES-256-GCM encrypted MongoDB snapshot (MongoDB 5.0+ replica set or sharded cluster). Use a unique strong passphrase prompted interactively or supplied securely through `SUMMON_BACKUP_PASSPHRASE`, and protect it separately from the `.sbak` file. Export is limited to 256 MiB encrypted and 1 GiB expanded. Known credential fields are omitted; the separate Wzgram session database, web sessions, nonces, rate limits and ingress leases are excluded. Free-text data can still contain secrets, and backups still contain personal and ownership data; treat them as sensitive. Legacy artwork blobs are included. This tool does not send backups to Telegram.

After authorization and with `MONGO_URL`/`MONGO_DB_NAME` configured, for example:

```bash
mkdir -p backups
.venv/bin/python backup.py export backups/summon.sbak --confirm EXPORT-ENCRYPTED
.venv/bin/python backup.py verify backups/summon.sbak
# Only after stopping all writers and selecting a separate, empty database:
.venv/bin/python backup.py restore backups/summon.sbak --target-database summon_restore_rehearsal --writers-stopped --confirm RESTORE-INTO-EMPTY
```

Restore authenticates and validates the complete file before writes, requires a separate empty database whose name contains `_restore_`, and uses insert-only writes; it never overwrites the source. An interrupted/failed restore can leave a partial target requiring operator review. A synthetic export/restore passed isolated MongoDB CI; the flow has not been rehearsed on production data or a production deployment. Validate recovery on an authorized isolated database before relying on it.

## Attribution and licenses

SUMMON's product and bot implementation is independently written. The reference repositories `egoist0690/Waifu-bot-`, `Beasgohan-code/Summon-bot`, `Beasgohan-code/Videl0`, and `Beasgohan-code/Videl1` were inspected read-only; no bot implementation or user data was copied. The reference MIT notice remains at `docs/licenses/Waifu-bot-reference-MIT.txt`.

The separately approved Richgram 0.2.0 heading/table/details builder subset is adapted from `Badmunda05/richgram` (commit `c95cc39`); its MIT notice is retained at `summon/_vendor/RICHGRAM_LICENSE`. Wzgram is LGPL-3.0-or-later and WarpCrypto is Apache-2.0; satisfy their full distribution terms and those of other dependencies.

The 978 supplied emoji glyph/ID pairs are retained across seven palettes. The user attributed the Emoji_Fan/EmojiPersian, rijif, Boseh/NewEmojis, News Emoji, Summon bot, and Telegram iOS lists to @TgEmojis. IDs do not contain artwork; attribution does not establish licensing, pack availability, bot entitlement, or permission. Verify rights and eligibility before use.
