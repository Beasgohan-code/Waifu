# Testing and release gates

## Executed checks — 2026-11-06

| Check | Result | Scope / limitation |
| --- | --- | --- |
| `pytest -q` in the locked dev environment | **574 passed, 17 integration tests skipped**, 1 warning | Includes mocked topology/readiness, account-isolation, logout/replay, aiogram dispatch, transcribed emoji fixtures and escaped/rich/photo/ephemeral fallbacks, synthetic migration checks, warning authorization/audit, public-note and auto-reply authorization/revision/race fakes, replay claims, production URL/error-redaction and owner-scoped collection filters. Real MongoDB tests were skipped locally; the previous revision passed isolated hosted CI as described below. Telegram and real-data migration tests were not run. Starlette warns that `httpx` TestClient integration is deprecated in favor of `httpx2`. |
| `ruff check summon tests` and `ruff format --check summon tests` | Passed | Static lint and formatting. |
| `mypy summon` | Passed, 48 source files | Type checks application code, not every dependency. |
| `uv lock --check`, clean locked installs, `uv pip check` | Passed | Linux x86-64/Python 3.11 only. Fresh development and non-editable runtime environments installed successfully; tampered runtime-wheel and setuptools build hashes were each rejected in separate isolated copies. Build-hash enforcement is experimental in pinned uv 0.13.7. |
| `pip-audit --skip-editable` | No known vulnerabilities | Audited the clean locked development environment; editable `summon-platform` is skipped. Separately bootstrapped uv/OS packages are outside this audit; results are time-dependent and do not prove safety. |
| High-confidence secret-pattern scan | No matches | Scanned 145 project text files for bot-token, private-key, GitHub-token and credential-bearing Mongo URI patterns, excluding local environments/dependencies and `.env`; this is not a complete secret audit. |
| `npm test` | **66 passed** | Vitest API, launch binding, theme, gift, group-goodbye, warning-review, notes and auto-reply editing/confirmation/draft/stale-result, collection filters and mocked Telegram-native button/version/fallback/navigation tests. |
| `npm run typecheck`, `npm run build` | Passed | TypeScript and production Vite build. |
| `npm audit` and `npm audit --omit=dev` | 0 vulnerabilities | Current lockfile / registry advisory state only. |
| Browser smoke against sample API/Vite | Passed | A stubbed Telegram bridge checks native shortcut string ID, Unicode-disabled fallback, navigation and dialog cleanup with no API writes. Also checks desktop navigation, search, duplicate filter and sort, focus-trapped dialog, read-only group controls, themes, 390px/320px layouts and touch targets. No live Telegram or real database. |
| Synthetic warning confirmation browser test | Passed | At 320px, checks confirmation focus, both themes, one scoped request and empty state; all warning API responses and the write were intercepted in the browser. No real warning was created or revoked. |
| Synthetic notes browser test | Passed | At 320px in dark/light themes, checks public notice, edit/create revisions, delete and draft confirmations, focus and layout. All note writes are intercepted; unexpected writes are aborted. No real notes were created or deleted. |
| Synthetic auto-replies browser test | Passed | At 320px in dark/light themes, checks public warning, disabled-by-default creation, enable/edit/delete, revision and conflict recovery, focused confirmations and layout. All writes are intercepted; no real rules or Telegram messages were created. |
| CI workflow static validation | Passed | actionlint 1.17.0 and ShellCheck 0.10.0 checked the updated `.github/workflows/checks.yml`. |
| Hosted CI, synthetic MongoDB/browser | **Previous revision passed; current changes pending** | [Run 42224451111](https://github.com/Beasgohan-code/Waifu/actions/runs/42224451111) passed all three jobs on the preceding revision, including 12 single-member replica-set tests and one standalone probe with zero skips. Four additional isolated Mongo tests are included in the current work but skipped locally. Containers use a test-only allocator setting; no production data or Telegram account was used. Check the current PR for the latest result. |
| Browser session-isolation smoke | Passed earlier in this session | Two tabs share a cookie jar; synthetic Telegram proofs and an in-memory store exercise account switching, proof replay, matching reload and copied-cookie logout revocation. Not a real Telegram or MongoDB integration test. |
| Offline migration preview | 50 synthetic tests passed; installed console entry point and `--help` checked | Exercises JSONL/Extended JSON, exact mappings, duplicate copies, collision/missing-data detection, optional target comparison, privacy, read-only local files and limits. No actual export, target database or importer was used. |

The refreshed sandbox had no system Chromium; a temporary compatible binary and library bundle were supplied for the browser smoke. An initial launch failed on missing shared libraries, then the configured command below passed. These temporary files are not part of the project.

Run browser smoke from `web` with the read-only API and Vite already running:

```bash
# Use a compatible locally installed Chromium; the sandbox needed these paths:
LD_LIBRARY_PATH=/tmp/lib CHROMIUM_PATH=/tmp/chromium npm run test:browser
LD_LIBRARY_PATH=/tmp/lib CHROMIUM_PATH=/tmp/chromium npm run test:warning-browser
LD_LIBRARY_PATH=/tmp/lib CHROMIUM_PATH=/tmp/chromium npm run test:notes-browser
LD_LIBRARY_PATH=/tmp/lib CHROMIUM_PATH=/tmp/chromium npm run test:auto-replies-browser
```

The `/tmp` paths are sandbox-specific and may disappear; install/provide Chromium on another machine. `SUMMON_PREVIEW_URL` can select a different preview URL. The preview smoke deliberately asserts that mutations are disabled. The separate warning, notes and auto-reply tests intercept bootstrap and their API responses **in their own browser pages**, including every mutation, so they exercise interactive confirmations without writing to the sample API, MongoDB or Telegram. The notes and auto-reply tests also abort other unexpected writes. Run these only against the isolated read-only preview, never against real data.

### Synthetic browser authentication smoke

This separate fixture uses **known fake credentials**, synthetic signed launches, sample data, an in-memory session/nonce store and a disabled limiter. It cannot validate MongoDB or Telegram. Run it only in an isolated development environment, never expose it to real traffic, and stop it afterward. Stop the read-only sample API before starting the fixture because both use port 8000.

```bash
# Terminal 1, from the repository root — isolated test only
SUMMON_SESSION_TEST_FIXTURE=1 .venv/bin/python tests/browser_auth_fixture.py
# Terminal 2: cd web && npm run dev
# Terminal 3, from web, with a compatible Chromium
CHROMIUM_PATH=/path/to/chromium npm run test:session-browser
```

The sandbox run additionally used `LD_LIBRARY_PATH=/tmp/lib`. The test intercepts Telegram's JavaScript and supplies fake launch data, then checks cross-account cookie isolation, same-launch reload, rejected proof replay and logout revocation. Stop the fixture and restart the read-only preview API before using the sample UI.

## Continuous integration workflow

`.github/workflows/checks.yml` defines read-only-permission jobs for Python lint/types/unit tests and dependency audit; frontend tests/build/audits and sample/intercepted browser smoke; and disposable local MongoDB 8.0 containers on a GitHub-hosted runner. The MongoDB job initializes a **single-member** replica set and a separate standalone, runs the opt-in tests using synthetic data, requires zero skips, and removes those disposable containers. It contains no production secrets or production database endpoints. The image and GitHub Actions are pinned; revisit updates and audits regularly.

The first hosted [run](https://github.com/Beasgohan-code/Waifu/actions/runs/42220018644) failed its browser smoke and MongoDB startup steps. A [diagnostic rerun](https://github.com/Beasgohan-code/Waifu/actions/runs/42220459194) showed that the browser test assumed a dark default while the live Telegram bridge supplied light, and MongoDB 8.0.34 refused the runner's kernel (SERVER-121912). The preview smokes now stub the remote bridge and choose their initial theme explicitly. The disposable test containers use `GLIBC_TUNABLES=glibc.pthread.rseq=1` and check that per-CPU allocator caches are disabled. All three jobs then passed in [run 42220885187](https://github.com/Beasgohan-code/Waifu/actions/runs/42220885187), including all 13 isolated MongoDB cases with no skips. This test-only allocator setting trades behavior for runner compatibility, not production validation; MongoDB's [production notes](https://www.mongodb.com/docs/manual/administration/production-notes/) recommend upgrading affected kernels. A single member does not prove multi-node majority loss, failover, production-scale contention, a hosted service's permissions, or real Telegram behavior. The runner/container configuration is test infrastructure, not a deployment template.

## Opt-in replica-set harness

`tests/integration/test_replica_set.py` contains sixteen real-database tests for concurrent claims, daily rewards, trade acceptance, gift acceptance/expiry, failed-spawn cancellation/rollback, cross-instance session revocation/expiry, concurrent launch-proof consumption, warning revocation races/rollback/legacy counts, public-note revision/cap races and audit rollback, auto-reply revision/cap/normalized-trigger races, schema and audit rollback, shared delivery claims, and owner-scoped duplicate sorting/pagination. All were skipped locally. Twelve older cases passed on the preceding hosted single-member replica-set run; the four new auto-reply cases still require a current hosted run. To run them on other infrastructure, an authorized operator must provision an isolated test replica set or sharded cluster and supply `SUMMON_TEST_MONGO_URI` through a protected environment/secret mechanism (do not paste the URI into shell history or logs). Then explicitly set `SUMMON_TEST_MONGO_CONFIRM=CREATE_ISOLATED_TEST_DB` and run `.venv/bin/pytest -q -m integration`. Each test creates a new random `summon_test_…` database and initializes validators, indexes and sample records. It never targets a user-selected database name.

By default those temporary databases are **not dropped**. If the operator explicitly approves their deletion, also set `SUMMON_TEST_MONGO_DROP_CONFIRM=DROP_ISOLATED_TEST_DB`; the harness only drops the randomly generated database from that test. Without both the URI and create confirmation, it will not connect/create data. These tests do not yet cover every race, rollback, failover or production permission scenario below.

A separate `tests/integration/test_standalone_probe.py` test performs only `ping` and the topology check (no schema or other writes) against an explicitly authorized standalone. It is skipped without `SUMMON_TEST_STANDALONE_URI`; an operator must also set `SUMMON_TEST_STANDALONE_CONFIRM=PROBE_STANDALONE_READ_ONLY` before it connects. Provide the URI via a protected secret mechanism, never in logs or chat. This probe was skipped locally and passed against the disposable hosted standalone.

## Remaining release blockers

The single-member CI run covers selected cases below with synthetic data. Repeat and expand them against the intended authorized topology and production-like workloads; the CI result is not a production guarantee.

1. **Real MongoDB transactions and sessions:** on an isolated replica set, race many claimants against one spawn; race gift/trade acceptance/cancellation; lose ownership mid-offer; test daily concurrent claims, warning revocation races, scoped cursors, old-warning counts, public-note and auto-reply cap/edit/normalized-trigger races, audit rollback and delivery claims, group-document contention at realistic activity rates, index/rollback and commit uncertainty, transaction retries/rollback, idempotent history, unique indexes and negative-quantity prevention. Verify real primary linearizable session reads, majority nonce/session writes, revocation across API workers, expiry and TTL behavior, latency, timeouts, failover and commit uncertainty. Verify standalone Mongo fails closed, and test schema permissions, existing-validator compatibility and any intended sharded/load-balanced topology.
2. **Real Telegram:** with a dedicated test bot/group, verify actual polling and delivery of new/edited messages under privacy mode, anti-spam command coverage and administrator exemption, current delete rights/revocation, warning-review/correction role changes and private responses, note member/admin role changes and escaped public replies, auto-reply exact matching/permissions/delivery/replay behavior, service/anonymous events and goodbye notices on both voluntary leaves and removals, callback actor authorization, rich HTML and compact tables, premium emoji and photo-caption fallback, the Mini App native collection button and its Unicode fallback, optional Wzgram's no-update connection, native keyboard callbacks, ephemeral command/reply/DM fallback, older clients and uncertain timeouts. Record Telegram/client versions. Telegram acceptance may not guarantee ephemeral delivery.
3. **Production boundary:** verify HTTPS and allowed Host/Origin behavior behind the actual proxy, Web Crypto support, forwarded binding headers, secure cookies, login replay rejection, shared-cookie Telegram account switching, revocation across real workers, API and proxy rate limits, request sizes, frontend same-origin routing, asset paths, error redaction and operational observability. Perform accessibility testing beyond the smoke checks.
4. **Scale and fairness:** test counts/skips and latency on realistic catalogue sizes and verify expected bucket weights/equal member selection with a stable catalogue. Test concurrent catalogue edits separately; exact fairness across edits is not guaranteed.
5. **Recovery and migration:** the offline JSONL preview is only a partial ownership comparison. Validate the real source schema and snapshot completeness; implement and test an authorized dry-run importer, full ownership/duplicate and target-catalogue reconciliation, backup, isolated restore, cutover and rollback. No production source data has been touched here.
6. **Release hygiene:** review and test the Python lock/build hashes on every intended platform, rerun dependency and bootstrap/OS audits at release time, validate notices and deployment configuration, address the TestClient deprecation, and obtain operator/security review.

No live deployment, production-readiness claim, full feature parity, production transaction guarantee or whole-bot performance claim follows from these local and isolated CI checks.
