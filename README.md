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

Catalog models recognize the 18 reference rarity tiers while retaining stored `epic` values; legacy Epic and Special Edition share one spawn bucket. Weights start with the reference's initialization defaults, including AMV 10; another reference fallback specifies 100, and a running reference instance can have customized values. Configured operators can use private `/chance rarity-number weight` to change a global spawn weight from 0 to 1,000,000; zero disables that bucket, and zeroing all buckets disables new spawns. Changes are transactionally audited, `/chancelist` shows current values, and in-flight selections may still use the prior weights. Only nonempty active buckets participate. `/hmode` changes only the sender's collection ordering or favorites filter. In groups, `/spawn` requires a current administrator with change-info permission and uses the same enabled, activity, cooldown, and active-summon gates as automatic spawns; it does not force a summon or count a message.

Configured operators can create characters in a private bot chat with `/upload id | name | series | rarity | HTTPS-URL`, or omit the URL and caption or reply to an ordinary Telegram photo. `/addchar` accepts the same details without an ID and generates one; `/updatechar id name|series|rarity|image|description value` (also `/update`) changes metadata, with a URL or replied-to photo for `image`. These commands do not transfer ownership. Photos must be ordinary JPEGs up to 3 MiB; protected, spoiler and self-destructing photos are rejected, and accepted photos are transcoded with metadata removed. Private `/owner` and `/panel` open the existing authenticated Mini App, and `/sudolist` shows the configured operators read-only; operator privileges are not editable through chat.

**Artwork is public.** Upload only images you have permission to publish. By default sanitized photo bytes are stored in MongoDB, and public responses can be cached for an hour. Optionally set both `ARTWORK_BACKEND=imgbb` and `IMGBB_KEY` (case-insensitive, including `Imgbb_key`; `IMGBB_API_KEY` also works) to send new Telegram photos to ImgBB instead. A key alone never enables it. `DB_CHANNEL` (or `DB_CHANNEL_ID`) may separately identify a negative Telegram channel ID; after a confirmed catalog save, the bot attempts to copy sanitized Telegram photos to that verified channel. It never copies URL-only entries. These options are SUMMON additions, not verified reference behavior, and have not been tested against live providers. External uploads and channel posts can be orphaned or ambiguous after failures; check the provider/channel before retrying. There is no automatic deletion workflow.

For HTTPS URLs, SUMMON does not fetch or verify the destination: Telegram and app clients fetch the URL, whose DNS or redirects can change. Operators must choose a trusted public host. The public image route rate-limits requests by the observed network host, so shared proxy egress can combine clients. Live Telegram, hosted proxy behavior, and production MongoDB transactions still need an authorized rehearsal.

Run one `summon-bot` worker per bot token and one or more `summon-api` workers. Configure the same bot token, application MongoDB database, separate `SUMMON_TELEGRAM_SESSION_DATABASE`, and session secret consistently. The bot must have read/write/index permissions for its dedicated session database; API workers need read access to its `peers` collection, not the session auth-key collection. That database and its backups contain credential-bearing MTProto session data and sensitive peer metadata. Restrict and encrypt access; never share one MTProto auth session concurrently across workers.

**Before starting the Wzgram worker**, stop all previous consumers and have an authorized operator remove any existing Bot API webhook while preserving pending updates according to the operator's cutover plan. Wzgram cannot inspect or remove that webhook. Only after verifying this, set `SUMMON_WEBHOOKS_DISABLED_CONFIRMED=true` on the bot worker. The confirmation is an operator assertion, not an automatic check. On first cutover, audit any queued Bot API updates separately; do not assume they were delivered to MTProto.

For group operations the API loads known peer access hashes from the worker's cache, then checks current Telegram membership/rights. Unknown or inaccessible peers fail closed rather than granting access; have the affected group/member interact with the bot and retry. Cross-session access hashes, fresh role changes, and API/worker startup and recovery require live verification.

A Render web service can run `summon-api` using `PORT`; a separate worker can run `summon-bot`. The Dockerfile can also serve `web/dist` on the same origin using `SUMMON_WEB_DIST`. On Vercel, set the project root to `web` and set the server-side `SUMMON_BACKEND_ORIGIN` to the Render API's HTTPS origin with no path, query, or credentials. `web/api/[...path].js` forwards same-origin `/api/*` requests and cookies to that origin, with a 64 KiB body limit. Configure Render's `SUMMON_ALLOWED_ORIGINS` to include the exact Vercel HTTPS origin, and set `SUMMON_WEBAPP_URL` to that frontend. The proxy does not trust client-supplied forwarding headers; IP-based sign-in throttles may aggregate users behind shared proxy egress. Vercel routing, Render networking, cookie behavior, and rate limiting require an authorized live rehearsal.

`/health/ready` checks database reachability/topology, not Telegram delivery. When explicitly enabled, the protected `/health/poller` endpoint now reports `recent_connection` or `no_recent_connection` based on a successful MTProto `GetState` RPC; this is **not** a poll, lease, delivery, or handler-health guarantee. Keep its dedicated monitoring token in a secret header only. External alerting and real outage/failover checks remain necessary.

## Migration and recovery safety

No production data, session, log, backup, restore, migration, or credential rotation was performed here. Treat credentials previously committed anywhere as exposed and rotate them through the authorized operator. Never send database URIs or backups through Telegram.

There is no importer. `summon-migration-preview` only analyzes explicitly authorized local JSONL exports and mappings; it does not connect, import, or prove that an export is complete. Protect exports and reports, and do not commit them. Before any actual migration or destructive restore: obtain explicit authorization and confirmation, stop source writers, create encrypted backups, verify an isolated restore, map IDs and quantities without collisions, reconcile ownership and totals, and rehearse rollback. Never overwrite, rename, drop, or dual-write the source as an implicit fallback.

## Attribution and licenses

SUMMON's product and bot implementation is independently written. The reference repositories `egoist0690/Waifu-bot-`, `Beasgohan-code/Summon-bot`, and `Beasgohan-code/Videl0` were inspected read-only; no bot implementation or user data was copied. The reference MIT notice remains at `docs/licenses/Waifu-bot-reference-MIT.txt`.

The separately approved Richgram 0.2.0 heading/table/details builder subset is adapted from `Badmunda05/richgram` (commit `c95cc39`); its MIT notice is retained at `summon/_vendor/RICHGRAM_LICENSE`. Wzgram is LGPL-3.0-or-later and WarpCrypto is Apache-2.0; satisfy their full distribution terms and those of other dependencies.

The 978 supplied emoji glyph/ID pairs are retained across seven palettes. The user attributed the Emoji_Fan/EmojiPersian, rijif, Boseh/NewEmojis, News Emoji, Summon bot, and Telegram iOS lists to @TgEmojis. IDs do not contain artwork; attribution does not establish licensing, pack availability, bot entitlement, or permission. Verify rights and eligibility before use.
