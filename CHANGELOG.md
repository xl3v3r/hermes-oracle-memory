# Changelog — `hermes-oracle-memory`

All notable changes to this project are documented in this file.

## [1.1.1] - 2026-11-07

### Changed
- **Sterile distribution / identifier scrub** (no provider, sync, or search behavior change): removed every environment-specific and person-specific identifier from the shipped tree — the two fleet hostnames (and any reference to the decommissioned third host), system / contributor usernames and their absolute build-home paths, the production OCI region + ADB service descriptors, and a guessed default DB-user value in docs. Docs and test fixtures now use placeholders and synthetic hosts (`hosta`/`hostb`/`hostc`, `/srv/fixtures/`). No credentials were ever in the tree; this removes their *appearance* plus all origin identifiers so the plugin is installable on any host without leaking where it came from.

### Fixed
- **Clean install-scan verdict** — the tree now clears the in-process Hermes plugin security scanner (plugin-guard-v12) with a **`safe` verdict: 0 critical, 0 high** (was `caution`, 2 high). The two HIGH findings were (1) the `md_link_exfil` pattern tripped by GitHub-Expressions interpolation inside a markdown link in `.github/workflows/cla.yml` (a prompt-injection/exfil trip-wire on a workflow file), and (2) a `hardcoded_secret` pattern tripped by a credential-shaped placeholder literal in `INSTALL.md`. Both removed; the remaining 30 findings are medium/low informational (reading own `.env`, self `git clone`, self `pip install`, `subprocess` interpreter probe, deliberate hostile-string test fixtures) and do not gate installation.

### Notes
- Single-current-notes convention maintained: this is `RELEASE_NOTES_1.1.1.md`; the v1.1.0.1 notes file was removed.
- Test suite unchanged at 241 passed / 2 skipped (fixture renames are assertion-consistent).
- Git *history* (40 pre-1.1.1 commits) still contains the old identifiers — only the installed/distributed tree (what the scanner reads) is scrubbed.

## [1.1.0.1] - 2026-11-07

### Fixed
- **Sync re-exec guard (interpreter selection)**: `scripts/sync_skills.py` now re-execs into a venv whose path provides `oracledb` (import probe, not realpath comparison), instead of failing with `Oracle memory pool failed: No module named 'oracledb'` when launched from a bare system/`tools`-python interpreter. Guarded by the `_HERMES_ORACLE_SYNC_REEXEC` env flag to prevent re-exec loops.
- **Canonical skill meta-row ID**: `format_skill_payload(mode="meta")` now emits `memory_id = skill:{host}:{name}:c0` (with `chunk_index: 0`) — identical to the full-mode chunk-0 row. The previous bare `skill:{host}:{name}` ID was invisible to the sync dedup select, the list predicate, and the lifecycle idempotency check, causing `ORA-00001` unique-constraint failures and broken idempotency on the second sync run.
- **DPY-4024 on bulk writes (call-timeout clamp)**: `_session_callback` previously set `conn.call_timeout = _SEARCH_TIMEOUT_MS` (13 s) on *every* pooled connection — including the fresh connections `_insert_many` acquisitions — so a single in-DB ONNX batch embedding that exceeded 13 s was killed with `DPY-4024` and the whole sync rolled back. The default per-connection budget is now `max(_SEARCH_TIMEOUT_MS, _INGEST_TIMEOUT_MS)`; read paths (health, search, skill fetch, list) set their tight 13 s window themselves after acquire, so interactive recall keeps fast-fail.
- **ONNX transient-error fallback**: embedding errors other than `ORA-51815`/`ORA-54401` (e.g. `ORA-01013`, `ORA-04031`, PGA/parallelism/OOM-class server hiccups) previously hit `_onnx_mark_failed()` (60 s process-wide cooldown + lost row) in the `_insert`/`_insert_many` ladders. They now degrade to the existing NULL-embedding + background-backfill path. The same transient gate now also covers the `oracle_update` (UPDATE ladder), which previously still had only the shape-error variant — the last write path missing it.
- **Pool fingerprint includes timeout config**: `initialize()` fingerprint is now `user@dsn:table:ct=<call_timeout_ms>` so long-lived processes re-create the pool when `HERMES_ORACLE_INGEST_TIMEOUT` / `HERMES_ORACLE_SEARCH_TIMEOUT_MS` change, instead of silently serving connections with a stale `call_timeout`.
- **Self-deadlock in live sync (`scripts/sync_skills.py`)**: the sync's main connection `DELETE`s replaced skill rows, then the provider's `_insert_many()` re-`INSERT`s the same `memory_id`s on a *second* pool connection, blocking on the first connection's own uncommitted delete locks (the first is parked inside the `_insert_many` call and never commits). Deletes are now committed before `_insert_many`, and any in-database error rolls the connection back before release so no uncommitted delete transaction leaks to the next run.

### Cleanup
- **Code audit / slop-removal pass**: three anachronistic `Tuple[str, dict]` annotations in `__init__.py` (undefined name — only safe because `from __future__ import annotations` defers evaluation — now builtin `tuple`), a dead `nonlocal` binding in the fallback YAML parser, and two unused `import sys` in maintenance scripts. Every one of the 72 broad `except` handlers was classified and confirmed best-effort with a functional fallback; all 30+ pool-acquire sites audited for nested holds (none); no redundant DB round-trips found in the search path.

### Tests
- `test_oracle.py` version-consistency test updated to 1.1.0.1 (VERSION, `__init__.py`, `plugin.yaml`, `pyproject.toml`, `skills/oracle-import/SKILL.md`, README, INSTALL, single current `RELEASE_NOTES_1.1.0.1.md`, `v1.1.0.1` tag-vs-master check, install-metadata revision).
- `test_sync_skills.py` meta-mode assertion updated to the canonical `skill:{name}:c0` ID.

## [1.1.0] - 2026-11-06

### Added
- **Shared-Corpus Read Path (`oracle_load_skill`)**:
  - Full skill discovery and reconstruction path (`action="fetch"` / `action="list"`) allowing any Hermes host to discover and load any skill across the 2,300+ fleet-wide library dynamically at runtime without local disk installation.
  - Word-accurate chunk reassembly reproducing source documents from 80-word, 10-word overlap chunks including bundled reference files.
  - Multi-variant resolution with explicit path and variant disambiguation.
  - Cost guard capped via `HERMES_ORACLE_SKILL_FETCH_MAX_CHUNKS` (default: 400).
- **Live Lifecycle Ingestion (`on_skill_lifecycle`)**:
  - In-process hook integration syncing skills into Oracle 26ai ADB immediately upon `created`, `edited`, `patched`, `installed`, or `restored` events.
  - SHA256 hash idempotency preventing redundant DB round-trips when skill contents are unchanged.
  - Safe retirement on `archived` events when local files are removed.
- **Zombie-Safe Host-Scoped Pruning (`scripts/sync_skills.py`)**:
  - Host-scoped pruning keyed on `(skill_name, metadata.path LIKE host_root)` pairs—foreign-host skills are strictly preserved, while locally deleted skills are cleaned up up to `--prune-limit`.
  - Host-tagged `memory_id` and `conv_ref` convention (`skill:{host}:{name}:cN`) preventing accidental sibling host overwrites.
  - Dead SQL leg 3 removed (`OR memory_id LIKE :3`); one-shot migration utility (`--migrate-host-tags`).
- **Resilient & Idempotent Initialization**:
  - Idempotent `initialize()` preventing duplicate connection pools and eliminating `pool not initialized` warnings during multi-context instantiation.
  - Stale connection detection and automatic re-acquisition for ADB restart recovery.
  - Provider health diagnostic reporting (`health()`).
- **Separated Ingest Timeout**:
  - Autonomous ingestion timeout (`HERMES_ORACLE_INGEST_TIMEOUT`, default 600s) decoupled from query search timeouts (`HERMES_ORACLE_SEARCH_TIMEOUT_MS`, default 1300ms).
- **New Skill**:
  - Added `skills/oracle-skill-corpus/SKILL.md` registered via `register(ctx)`.

### Docs
- Bumped canonical version to `1.1.0` across `VERSION`, `__init__.py`, `plugin.yaml`, `pyproject.toml`, `README.md`, `INSTALL.md`, `AGENTS.md`, `NEEDS.md`, `CHANGELOG.md`, `skills/oracle-import/SKILL.md`, and `skills/oracle-skill-corpus/SKILL.md`.
- Maintained single current release notes file `RELEASE_NOTES_1.1.0.md`.

## [1.0.9.6] - 2026-11-01

### Fixed
- **Skill Retrieval Starvation (P0)**:
  - Removed `ORDER BY CASE WHEN role = 'fact' THEN 1 ELSE 2 END ASC` from the top-k cut of `_build_hybrid_search_sql` (both ONNX and fallback branches), `_build_vector_search_sql` (both branches), and `_build_text_search_sql`.
  - Root cause: the role key sorted ahead of the similarity key before `FETCH FIRST :k`, so with ≈1,379 `role='fact'` rows and `k = 2 * top_k = 10`, the candidate window was always filled by facts and **no `role='skill'` row could ever be returned** — 31,734 skill chunks across 2,301 skills were unreachable in hybrid, vector and text mode.
  - Candidates are now selected purely by similarity (`ORDER BY distance ASC` / `ORDER BY txt_score DESC`), so the skill index participates in retrieval again.
- **Post-Fusion Role Weighting (P1)**:
  - Role preference moved fully into `_WEIGHT_ROLE`, which is applied after candidate selection.
  - Added `skill: 0.85` (below `fact: 1.0`, above `turn: 0.75`): a clearly closer skill chunk outranks a weaker fact, while at comparable relevance first-person facts still win — no starvation, no skill flooding of factual recalls.
- **Wider Candidate Pool (P1)**:
  - `_search` now requests at least 50 candidates per branch (`_MIN_CANDIDATE_POOL`, env `HERMES_ORACLE_MIN_CANDIDATE_POOL`) instead of `2 * top_k`.
  - Rationale: role weighting, prefix boosting and diversification all run after the cut, so a narrow window let the corpus densest in embedding space monopolise results — the structural cause behind the starvation bug (a bare `HANDOFF` query returned seven chunks of one unrelated skill).
- **Per-Source Diversification (P1)**:
  - Added `_diversify_rows()` and `_MAX_CHUNKS_PER_SOURCE` (env `HERMES_ORACLE_MAX_CHUNKS_PER_SKILL`, default 2): a single skill may contribute at most 2 chunks per result set.
  - Source identity comes from `conv_ref` when present, otherwise from the `[skill_ingest] [skill:<name>]` prefix; rows with neither (facts, turns) are never capped.

### Tests
- **Regression Coverage (P1)**:
  - `test_sql_builders_select_candidates_by_similarity_not_role` — asserts no builder ranks by role before `FETCH FIRST`, and that every branch orders by its similarity key.
  - `test_skill_rows_weighted_below_facts_but_never_starved` — asserts skill weighting sits between fact and turn, that a closer skill chunk wins, and that facts still win at equal relevance.
  - `tests/verify_provider.py` gained a live skill-reachability guard: a skill query must return `[skill_ingest]` rows or verification fails.
  - `test_skill_bundles` now skips (instead of failing) on hosts where curated bundles / the CaseMark archive were never deployed.
- **Test Script Consolidation (P2)**:
  - Removed the duplicate `scripts/run_tests.py`; `tests/run_tests.py` is the single test runner.
  - Moved `scripts/verify_provider.py` → `tests/verify_provider.py`; `scripts/` now holds operational utilities only.
  - `test_all_test_scripts_directory_structure` enforces that test/verification scripts live in `tests/` and not in `scripts/`.

### Docs
- Bumped canonical version to `1.0.9.6` across `VERSION`, `__init__.py`, `plugin.yaml`, `pyproject.toml`, `README.md`, `INSTALL.md`, `AGENTS.md`, `NEEDS.md`, `CHANGELOG.md`, and `skills/oracle-import/SKILL.md`.
- Refreshed the criticality notices, the "What's new" section, and the verification instructions (now `python3 tests/verify_provider.py`).
- Rewrote `NEEDS.md` to reflect resolved items 1-6 and the one remaining per-host deployment decision.
- Maintained single current release notes file `RELEASE_NOTES_1.0.9.6.md`.

## [1.0.9.5] - 2026-11-01

### Fixed & Fortified
- **Read Path ORA-51815 Retry & Graceful Degradation (P1)**:
  - Added 3-attempt backoff with 0.5s increments (`_51815_BACKOFF_SECS * (attempt + 1)`) for `_execute_hybrid_search` and `search(mode="vector")` on transient snapshot conflicts (`ORA-51815`).
  - Gracefully degrades hybrid search to Oracle Text search on exhausted retries rather than raising `RuntimeError`.
- **Vector Index Maintenance Path & DDL (P1)**:
  - Added canonical `VECTOR_INDEX_DDL` constant in `__init__.py`.
  - Provided `scripts/rebuild_index.py` maintenance utility to cleanly drop and recreate vector indexes with `DBMS_STATS.GATHER_TABLE_STATS` stats gathering.
  - Documented that `ORA-51815` requires waiting/retrying rather than rebuilding.
- **Batched DELETE Pre-Pass (P2)**:
  - Collapsed serial delete queries into `executemany` batches of 500 records.
  - Scoped deletions strictly to existing DB skills in the target sync set, preventing round-trip latency and protecting external corpora.
- **Exposed Ingestion Batch Size (P2)**:
  - Exposed `--batch-size` (default 50) across `scripts/sync_skills.py` CLI, `OracleMemoryProvider.sync_skills()`, tool dispatcher, and tool schema.
- **References Directory Chunking (`references/`) (P2)**:
  - Discovers text reference files (`.md`, `.txt`, `.rst`) under `references/` alongside `SKILL.md`.
  - Sequentially chunks reference files with `conv_ref=skill.name` continuing after body chunks and computes composite SHA-256 hashes.
- **Local Skills Full Sync & Verification (P1)**:
  - Fully ingested `~/.hermes/skills`, verifying 40 chunks total for `pro-se-litigation` with `references/mississippi-procedure.md`.
  - Added verification checks comparing DB chunk counts against `format_skill_payload(skill, mode='full')`.
- **Curated Bundle Manifest Symlinks (P1)**:
  - Created curated symlinks under `~/.hermes/skills/casemark/` for the 150 unique Casemark skills referenced across the 17 manifests in `~/.hermes/skill-bundles/*.yaml`.

### Added
- Dedicated unit tests for read path ORA-51815 retry and degradation, vector index DDL, rebuild script execution, reference directory indexing, and batch delete chunking.

### Changed
- Bumped canonical version to `1.0.9.5` across `VERSION`, `__init__.py`, `plugin.yaml`, `pyproject.toml`, `README.md`, `CHANGELOG.md`, `INSTALL.md`, and `skills/oracle-import/SKILL.md`.
- Enforced 100% zero drift between the build checkout, `~/.hermes/plugins/oracle`, and all profile environments.
- Maintained single current release notes file `RELEASE_NOTES_1.0.9.5.md`.

## [1.0.9.4.1] - 2026-10-30

### Fixed & Fortified
- **Retrieval Weighting & Re-Ranking**:
  - Implemented dynamic post-retrieval weighting (`_apply_weights`) incorporating exponential recency decay (`0.85 + 0.15 * exp(-age_days / 120)`), role weighting (`fact: 1.0`, `user: 0.9`, `session: 0.9`, `turn: 0.75`, `assistant: 0.75`), telemetry dampening (`0.05` for `[skill_usage]`, `[skill_vectorization*]`), and short content penalty (`< 25 chars -> 0.9`).
  - Added candidate `created_at` batch PK resolution chunked at 500 records with clean unweighted fallback on database errors, robust cursor lifecycle management on pool connection acquisition, and flexible support for `datetime.date`, ISO strings, and unix epoch timestamps.
  - Hardened sorting against `NoneType` and invalid scores, non-dict elements, and tuple input collections.
  - Hardened `_JUNK` filter across text, vector, and hybrid SQL query builders with telemetry filter to keep lifecycle telemetry out of retrieval candidate sets.
- **Test Harness Robustness**:
  - Guarded `test_zero_drift_with_installed_plugin` against `repo_dir.resolve() == plugin_dir.resolve()` to prevent `shutil.SameFileError` when tests run directly from the installed plugin directory.
  - Hardened file synchronization across repository, installed plugin, and profile environments to track and sync all files including `CLAUDE.md`.

### Added
- Comprehensive unit tests in `tests/test_oracle.py` verifying recency decay, role weighting, telemetry penalty, short content penalty, batch database lookup, date and unix timestamp support, none/corrupt score sorting resilience, cursor lifecycle, and graceful error fallback.

### Changed
- Bumped canonical version to `1.0.9.4.1` across `VERSION`, `__init__.py`, `plugin.yaml`, `pyproject.toml`, `README.md`, `CHANGELOG.md`, `INSTALL.md`, and `skills/oracle-import/SKILL.md`.
- Enforced 100% zero drift between the build checkout, `~/.hermes/plugins/oracle`, and all profile environments.

## [1.0.9.4] - 2026-10-30

### Fixed & Fortified
- **Search Ranking & Turn Noise Filtering**:
  - Prioritizes durable `role='fact'` entries over generic noise and sorts by recency (`created_at DESC`) across all text, vector, and hybrid search SQL queries.
  - Normalizes text score against standard Oracle Text max score (`min(1.0, txt / 100.0)`).
  - Implements exact prefix match boosting (`_is_prefix_match`) with word-boundary and stop-word validation, adding a `+0.15` relevancy boost to memories whose titles or content prefixes match the search query across hybrid, text, and vector recall modes.
  - Hardens `_JUNK` filter with `NVL(role, 'fact') NOT IN ('turn', 'assistant', 'session')` to prevent conversation turn and session noise from polluting retrieval results.
- **In-Database ONNX Insert & Update Fortification**:
  - Retries transient snapshot conflicts (`ORA-51815`) in `_insert`, `_insert_many`, and `oracle_update` up to `_EMBED_MAX_RETRIES` with progressive backoff.
  - Retries sequence length violations (`ORA-54401`) with a conservative token clamp (`_CONSERVATIVE_EMBED_CHARS = 400`), guaranteeing tokens stay safely below 512 across `_insert`, `_insert_many`, `oracle_update`, and `_embed_many_tagged`.
  - Enqueues rows into `_queue_embed_backfill` on any fallback insert/update with NULL embedding to guarantee asynchronous background embedding computation without turn loss.

### Added
- Unit tests covering ORA-54401 conservative token clamp retry and success, ORA-51815 transient snapshot conflict retry and success, oracle_update fortification, vector search prefix boosting, and background backfill queueing on null fallback in `tests/test_oracle.py`.
- Automated live verification checks for prefix ranking and turn noise exclusion in `scripts/verify_provider.py`.

### Changed
- Bumped version to `1.0.9.4` across `__init__.py`, `VERSION`, `plugin.yaml`, `pyproject.toml`, `skills/oracle-import/SKILL.md`, `README.md`, `INSTALL.md`, and test suites.
- Enforced 100% zero drift across repository, installed plugin (`~/.hermes/plugins/oracle`), and all profile environments.
- Replaced previous release notes with `RELEASE_NOTES_1.0.9.4.md` per release documentation policy.

## [1.0.9.3.2] - 2026-10-29

### Fixed
- **Defect 1 (`plugin_root` structural discovery)**: Replaced static 3-level parent traversal with `_find_plugin_root` upward directory scanning for `plugin.yaml` and `__init__.py` in `skills/oracle-import/scripts/import_conversations.py`. Restores bulk conversation and archive migration without `FileNotFoundError`.
- **Defect 2 (Provider import fallback chain)**: Fortified provider dynamic import fallback chain in `import_conversations.py` to catch all exceptions, verify file existence, and emit diagnostic path logging.

### Added
- Unit test suite `TestOracleImportScript` in `tests/test_oracle_import.py` for structural discovery and fallback error branches.

### Changed
- Bumped version to `1.0.9.3.2` across `__init__.py`, `VERSION`, `plugin.yaml`, `pyproject.toml`, `skills/oracle-import/SKILL.md`, `README.md`, `INSTALL.md`, and test suites.
- Purged previous historical release notes files prior to v1.0.9.3.2 per release policy.
