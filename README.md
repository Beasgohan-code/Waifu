# SUMMON ✦

**Collect. Discover. Summon.**

An async Telegram character-collection bot, group-management backend, and mobile-first Mini App. SUMMON is an original implementation informed by read-only review of the reference projects; their bot implementations and user data were **not copied**. A small MIT-licensed Richgram builder subset was adapted with attribution, as separately agreed.

> **Status: prototype, not deployment-ready.** Local checks and a disposable single-member MongoDB CI suite pass, but production/multi-node behavior, Telegram delivery, premium-emoji eligibility, a production proxy, backup restoration, and legacy-data migration have not been verified. No upstream repository or user data was changed. See [testing and release gates](docs/TESTING.md) and [the tracker](todo.md).

## What is here

- **Collection:** activity-triggered group spawns, name guesses, duplicate quantities, rarity tiers, favourites, wishlists, catalogue search, profiles, and leaderboards. The Mini App can show only characters with extra copies and sort the full inventory by copy count or when the inventory record was first added. Groups opt in to spawning. Points and XP are earned through catches and daily check-ins; there is no betting, wagering, deposit, cash-out, or casino.
- **Trading and gifts:** offers expire after 15 minutes; only the intended recipient can accept. Ownership is rechecked and the transfer, offer state, and history are written in a MongoDB transaction. Selected concurrent transfers pass against a disposable single-member replica set; production failover and broader contention remain unverified.
- **Group tools:** live Telegram administrator and bot permission checks for moderation, configurable spawns, welcome/goodbye messages and rules, and link, flood, duplicate, mention and keyword controls. Admins with change-info rights can manage public text notes and opt-in exact-match auto replies with revision checks; members can request notes with `/note`. Auto replies start disabled and depend on ordinary messages being delivered to the bot. Anyone who can see group messages may read or share the content, so never save secrets there. Restrict-authorized admins can review and explicitly revoke active warnings; corrections retain audit records and do not alter Telegram restrictions. Protection runs before delivered group commands and checks edits, subject to Telegram delivery and content limits. Warning escalation and several advanced workflows remain open.
- **Mini App:** Telegram-themed responsive views for the catalogue, collection, progression, trades/gifts, group controls and authorized catalogue operators. Signed Telegram launches are bound to revocable server-side sessions so different launches sharing browser cookies do not silently see each other's accounts. The local sample preview is visibly read-only.
- **Telegram presentation:** aiogram is the only update ingress. Native Bot API rich messages work without MTProto credentials; optional Wzgram is outbound-only. Adapted Richgram builders supply headings, compact tables and details. The registry retains 978 user-supplied glyph/ID pairs from seven palettes; selected icons appear in relevant cards, photo captions and buttons. A Telegram-native Mini App collection shortcut can also use a supplied icon on supported clients, while ordinary navigation remains available. Set `SUMMON_PREMIUM_EMOJI_ENABLED=false` for Unicode fallbacks. Pack rights, eligibility and live rendering have not been verified.
- **Offline migration preview:** inspect authorized legacy JSONL exports, explicit ID mappings and an optional target inventory snapshot without connecting or writing. It is not an importer or proof of migration readiness; see [migration and backup](docs/MIGRATION_BACKUP.md).

## Try the read-only preview

Requires Python 3.11+ and Node.js 22+. Use two terminals from the repository root:

```bash
# Keep the lockfile tool separate from the project environment it manages.
python3 -m venv .bootstrap
.bootstrap/bin/python -m pip install --upgrade 'pip==26.3' 'setuptools==84.2.1' 'uv==0.13.7'
.bootstrap/bin/uv sync --locked --extra dev --python 3.11 --no-python-downloads
cd web && npm ci && cd ..

# Terminal 1: sample API, no database or Telegram credentials used
SUMMON_PREVIEW_MODE=true .venv/bin/uvicorn summon.api.app:app --host 0.0.0.0 --port 8000 --no-access-log

# Terminal 2: frontend; /api is proxied to the sample API
cd web && npm run dev
```

Open the Vite URL shown in the terminal. The preview contains fictional examples, rejects all API mutations, and cannot be enabled when `SUMMON_ENVIRONMENT=production`. It is not a test of a real Telegram account or database. `uv.lock` pins direct and transitive Python dependencies; plain `pip install -e '.[dev]'` does not enforce that lock. See [deployment](docs/DEPLOYMENT.md) for production installs and the build-hash/platform limits.

## Run against your own infrastructure

Read [deployment](docs/DEPLOYMENT.md) and [migration / backup](docs/MIGRATION_BACKUP.md) first. A MongoDB **replica set or sharded cluster**, a bot token, an HTTPS Mini App origin and a high-entropy session secret are required. Startup and readiness reject clearly unsupported transaction topologies, but that check does not prove transactions, session consistency or permissions work. Configure secrets only in the runtime environment or a private `.env` copied from `.env.example`; never place them in frontend code, Git, Telegram messages or logs. Start exactly one `summon-bot` process per token and one or more `summon-api` processes. Serve `web/dist` and proxy `/api` on the same HTTPS origin. Existing pre-upgrade cookies are invalidated; users must reopen the Mini App from Telegram.

The optional example catalogue is **not** installed automatically. `summon-seed` alone prints a plan and does not connect. With an explicitly chosen database and operator approval, `summon-seed --apply --confirm INSERT-SAMPLE` inserts only missing sample IDs; it does not overwrite catalogue entries or touch ownership. Applying also runs normal database validator/index initialization. No legacy importer is implemented or has been run.

## Checks

```bash
.bootstrap/bin/uv lock --check
.venv/bin/pytest -q
.venv/bin/ruff check summon tests
.venv/bin/mypy summon
.venv/bin/python -m pip check
.venv/bin/pip-audit --skip-editable
(cd web && npm ci && npm test && npm run typecheck && npm run build && npm audit)
```

The browser smoke tests need a compatible Chromium executable and separate preview or synthetic-auth fixtures; see [testing](docs/TESTING.md). Results recorded there distinguish mocked tests from real infrastructure.

## Important owner actions

The read-only audit found previously committed credentials and an unsafe backup command in the **reference** `egoist0690/Waifu-bot-` deployment. If it is running, disable `/mongobackup`, restrict database access, rotate/revoke every exposed credential and session, inspect logs, and follow GitHub's secret-removal guidance. This session cannot perform or confirm those actions on the reference repository. Do not reuse exposed values.

## Documentation

- [Repository audit](docs/REPOSITORY_AUDIT.md) · [Reference inventory](docs/REFERENCE_MODULE_INVENTORY.md)
- [Architecture](docs/ARCHITECTURE.md) · [Database schema](docs/DATABASE_SCHEMA.md)
- [Telegram, Richgram & Wzgram compatibility](docs/TELEGRAM_COMPATIBILITY.md) · [Commands](docs/COMMANDS.md)
- [Deployment](docs/DEPLOYMENT.md) · [Migration and backup](docs/MIGRATION_BACKUP.md) · [Testing](docs/TESTING.md)
- [Third-party notices](docs/THIRD_PARTY_NOTICES.md) · [Living tracker](todo.md)
