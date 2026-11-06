# SUMMON repository audit

Audited 2026-11-02. This is an evidence-based starting point, not a claim of production readiness.

## Source of truth

The current checkout, `Beasgohan-code/Waifu`, began at `a49e74a` with exactly one tracked file, a README containing only `# Waifu`. There were no handlers, models, dependencies, tests, web app, data migrations, or deployment files. Nothing in the reference repositories is an existing feature of this checkout, and no existing data has been migrated.

## Feature references

- [`Beasgohan-code/Summon-bot`](https://github.com/Beasgohan-code/Summon-bot), `main` at `504fe36`: a `python-telegram-bot` application using synchronous SQLite/PostgreSQL storage. `main.py` registers collection, spawn, profile, daily, moderation and admin workflows. `database.py` has SQL users, characters, user_collection, warnings, groups, streaks, achievements, inventories, activity and marketplace tables. `auto_spawn.py` and `catch_all.py` both contain message-count/spawn paths; active spawns are held in process memory. The repository documents its SQL migration concerns; it does not implement MongoDB as the primary store. It has smoke and storage tests but no tracked Mini App frontend. Its tracked `bot.session`, `summon.db`, `bot.log`, bytecode and backups must **not** be copied. If the Telegram session is still valid, revoke it, rotate affected credentials and remove sensitive artifacts from the source repository's Git history. A `.gitignore` alone cannot remove history. Inspect and migrate user data only with the operator's explicit authorization and a verified backup.
- [`egoist0690/Waifu-bot-`](https://github.com/egoist0690/Waifu-bot-), `main` at `d2c3bce`: group activity spawns characters; players guess, collect, favorite, search, and propose trades. It initializes Kurigram, aiogram, `python-telegram-bot`, and Motor together. Spawn and trade state includes in-process dictionaries, and the trade swaps two collections through separate writes. Its character picker loads the complete allowed catalog. Those patterns are unsuitable for distributed, atomic ownership. Casino and wagering features are explicitly out of scope. No tracked web frontend was found despite the README's dashboard claim. MIT copyright © 2026 MrZyro; SUMMON does not import its bot code, and the reference license notice is retained for attribution.
- [`Beasgohan-code/Videl0`](https://github.com/Beasgohan-code/Videl0), `arena/042d6599-videl0` at `613efaa`: a MongoDB-backed Telegram music/group-management bot and separate Next.js app. Its moderation, fresh Telegram admin checks, signed Mini App identity, command presentation, premium emoji catalog and rejection fallback informed SUMMON's design. Its test and compatibility documents distinguish mocked checks from real Telegram eligibility. The Videl-specific bot, music functionality, owner ID, credentials and deployment are not SUMMON's implementation. No root license was found; do not copy its source wholesale. Selected user-supplied emoji identifiers are metadata, with original pack attribution in the emoji module.
- [`Badmunda05/richgram`](https://github.com/Badmunda05/richgram), `main` at `c95cc39`: provides MIT-licensed builders and a sender designed for Kurigram. Its distribution conflicts with Wzgram via the shared Pyrogram namespace; SUMMON vendors only an attributed, adapted builder subset and uses its own definite-rejection-only sender. No live rendering is claimed.
- [`kurigram-org/kurigram`](https://github.com/kurigram-org/kurigram), `main` at `77212d0`: an async **MTProto** client, not an aiogram Bot API poller. Its `Client(no_updates=True)` disables incoming updates. The user later selected Wzgram instead.
- [`rjriajul/wzgram`](https://github.com/rjriajul/wzgram), `main` at `016ce19`, installed 3.1.6: a Pyrogram-compatible MTProto fork using WarpCrypto. SUMMON uses it only outbound with `no_updates=True`, no handlers and no competing poller. Wzgram's sender takes `rich_text`, not Richgram's Kurigram-specific `rich_message`; see the compatibility matrix.

## Priority findings and decisions

These are original design decisions informed by reference behavior, not copies of reference bot implementations. The selected Wzgram and separately approved Richgram builder exception are described in [compatibility](TELEGRAM_COMPATIBILITY.md) and [notices](THIRD_PARTY_NOTICES.md).

1. **P0 — identity and authorization.** Validate Telegram Mini App `initData` on the server, expire it, reject duplicate fields and do not trust `initDataUnsafe`, caller-supplied user IDs or buttons. Recheck Telegram group permissions at each privileged operation. Protect secrets and audit administrative changes.
2. **P0 — ownership and money consistency.** Use one MongoDB source of truth. Collection claims, trades and rewards need atomic operations / transactions on a replica set; do not silently downgrade to non-atomic writes. Model inventory by `(user_id, character_id)` with unique indexes, preserve duplicates as counts, and persist spawn and trade state.
3. **P0 — ingress and API reality.** aiogram owns Bot API updates. The selected Wzgram connection is optional and outbound-only with `no_updates=True`. Enable rich, custom emoji and advanced features only when the installed client actually supports them and Telegram accepts them; retain readable fallback. Never retry ambiguous send failures as if the message definitely failed.
4. **P1 — group safety.** Permission-aware moderation, logs, flood/link/duplicate protection and explicit group settings before destructive automation. No silent authorization or privilege escalation.
5. **P1 — useful product slices.** Ship real collection, daily rewards and a connected Mini App incrementally; show honest empty/error states. No wagering, casino, deposits or cash-out.
6. **P1 — operational safety.** Index queries, timeouts, bounded inputs, structured logs without secrets, health checks, shutdown, documented backups, and tests. Motor was deprecated on 2026-05-14; use PyMongo Async. MongoDB transactions require a replica set or sharded cluster.
7. **P2 — compatibility/migration.** Original SQL data and feature parity are not automatically imported. A future migration needs a field mapping, dry-run, backup, reconciliation and rollback; do not dual-write MongoDB and SQL.

## Research and environmental limits

Telegram's [official 2026 changelog](https://core.telegram.org/bots/api-changelog#august-24-2026) lists Bot API 10.3 rich buttons, ephemeral message parameters, compact tables, expandable quotes and draft controls. Telegram's [Mini App documentation](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app) specifies the HMAC and timestamp checks. Its [formatting documentation](https://core.telegram.org/bots/api#formatting-options) limits custom-emoji use by bot eligibility and context; IDs alone do not guarantee rendering. The official MongoDB [migration guide](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/reference/migration/) recommends PyMongo Async over Motor.

The sandbox has Python 3.11 and Node.js 22; `mongod` and Docker were not found at audit time. Real Telegram credentials and a MongoDB replica set have not been supplied. Live Telegram delivery, transaction behavior against a real cluster, deployment, custom-emoji entitlement and migration must not be claimed without verification.

## Detailed upstream security and execution findings

These findings refer to the **reference** repository at `d2c3bce`, not files in the current checkout. The reference was inspected read-only; it has not been patched or deployed by this session.

| Severity | Evidence | Finding / action |
| --- | --- | --- |
| Critical | `config.py:8–16,36` | Literal defaults include a bot token, Telegram API hash, MongoDB URI and image-host API key. Assume these credentials are compromised. The owner must rotate the bot token, MongoDB credentials and image-host key, and replace/revoke the Telegram application credentials where supported. Examine other historically committed secrets and rotate them too. Do not repeat their values in issues, chat or logs. Removing current defaults does not erase Git history. |
| Critical | `TEAMZYRO/modules/mongo_backup.py:20–75` | `/mongobackup` has no visible owner/sudo filter, accepts and echoes source and destination MongoDB URIs, reads entire collections synchronously and calls `delete_many({})` on the destination without confirmation. Disable this command immediately on any running upstream deployment. Restrict database access and inspect audit logs/backups. SUMMON deliberately does not copy it. |
| High | `TEAMZYRO/__init__.py:75–98`, `TEAMZYRO/__main__.py:22–56`, `TEAMZYRO/modules/message_handler.py` | Three Telegram frameworks are initialized, but the checked-in startup only starts Kurigram. The python-telegram-bot `Application` handlers, including its message counter, are registered but no start/polling call was found. Their actual behavior must not be assumed to work. |
| High | `TEAMZYRO/unit/zyro_send_img.py` | The spawn sender fetches the full character catalog and assigns `last_characters[chat_id] = character`, although selection uses `selected_character`; the referenced `character` is not defined in that function. This path is also tied to the inactive application above. |
| High | `TEAMZYRO/modules/guess.py`, `trade.py`, `TEAMZYRO/__init__.py` | Active spawns, first guesses and pending trades are stored in process dictionaries. Catch and trade operations use multiple separate writes. Restarts lose state and concurrent transfers can diverge. |
| Medium | `TEAMZYRO/modules/upload.py:290`, `rarity.py:6` | `/rarity` is registered in two feature modules. Broad callback prefixes and global wildcard imports also make collisions/reachability hard to reason about. |
| Medium | `Dockerfile`, `runtime.txt`, `requirements.txt` | Docker targets Python 3.9 whereas the runtime file targets 3.11; requirements are mostly unpinned, include three Telegram frameworks and duplicate APScheduler. No tracked automated test suite or frontend is present. |
| Medium | `TEAMZYRO/__main__.py:34–41` | Database initialization exceptions are logged and startup continues, despite indexes and persistence being critical. Some exception paths print exception details that can contain sensitive URLs. |

The [module inventory](REFERENCE_MODULE_INVENTORY.md) enumerates every reference Python module using static analysis. Feature presence below is evidence of source code, **not** proof that its handler is reachable or works live. The complete semantics of all legacy games, owner commands and callbacks still need an individual migration/security review before any are ported.

| Reference workflow | Evidence | SUMMON status |
| --- | --- | --- |
| Guess, spawn, collection/harem, search, favorites | `guess.py`, `message_handler.py`, `harem.py`, `search_characters.py`, `favorites.py` | Independently implemented in part with persistent MongoDB state, bounded queries and transactional claims; live cluster/Telegram verification pending. |
| Trade and gift | `trade.py`, `gift.py`, `transfer.py` | Two-party trades and recipient-confirmed gifts have transaction-backed implementations and mock tests; real-cluster verification and legacy import remain open. |
| Characters, rarities, uploads | `addchar.py`, `upload.py`, `update.py`, `rarity.py` | Validated catalog API and weighted rarities implemented; media uploads and legacy migration remain open. |
| Profiles, rank, stats, leaderboards | `balance.py`, `stats.py`, `leaderboard.py`, `rank.py` | Profile, daily points, XP and leaderboard implemented; more analytics and historical migration remain open. |
| Owner/sudo, broadcast, backup | `sudo.py`, `eval.py`, `broadcast.py`, `mongo_backup.py` | Not blindly ported. Catalog operator access is server-side. Remote eval and destructive URI-based backups are intentionally excluded. |
| Casino/wagering | `blackjack.py`, `coinflip.py`, `mines.py`, `slot.py`, `wheel.py`, `high_low.py`, betting portions of `tic_tac_toe.py` | Intentionally excluded by the product's non-gambling requirement. |
| Other character events and utilities | `demonslayer.py`, `redeem.py`, `valentine.py`, `bounty.py`, `shop.py`, `inlinequery.py` | Not yet ported; assess usefulness, safety, licensing and non-wagering requirements individually. |

### Scope and baseline

The new repository contained no pre-existing application tests, database, code or credentials. This is a new implementation informed by the reference, **not** an in-place upstream refactor and not yet feature parity. No production data has been read or changed. No real bot token, MongoDB cluster, or Telegram administrator rights have been used in tests. Exact checks executed for this checkout are recorded in `todo.md` and the final report.
