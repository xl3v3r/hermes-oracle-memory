## [1.1.2] - 2026-11-18

- **Per-user memory scoping**: scope_id column, HERMES_ORACLE_SCOPE_MODE, GSK validation, skill sync gating (see RELEASE_NOTES_1.1.2.md)
- **Issue #16 fixed**: No more cross-user memory leakage
- **Issue #17 implemented**: Per-user memory scoping for multi-user frontends
- **Issue #18 fixed**: Text index DDL failure handling improved
- **Issue #25 fixed**: Wheel now includes scripts/ directory
- **Issue #14 resolved**: Skill identity follow-ups with shared host id and skipped_foreign override
- **Version bump**: 1.1.1.5 → 1.1.2

# Changelog — `hermes-oracle-memory`

All notable changes to this project are documented in this file.

## [1.1.1.5] - 2026-11-15

Follow-ups to the 1.1.1.4 review (#14).

### Added
- `replace_foreign`: `oracle_sync_skills(replace_foreign=true)`, `sync_skills.py --replace-foreign` and `sync_skills_library(replace_foreign=True)` replace this host's copies stored from SKILL.md files outside the profile's skill dirs, after a warning. A sync that skipped such copies returns a `hint`.
- `scripts/sync_skills.py`: `walk_skill_tree()`, `is_live_skill_file()`, `profile_skill_roots()` and `IGNORED_SUBDIRS`, shared by the bulk sync and the lifecycle hook.
- Tests: `test_skill_followups.py`, `test_link_guard.py`; a runtime link guard in `tests/conftest.py`.

### Fixed
- **Foreign copies**: the bulk sync judges ownership against the profile's own skill dirs (its defaults plus `dirs`), not just the dirs a run scans, so a sync over a `--dirs` subset updates the copies it stored from the other dirs instead of skipping them as foreign. A `--dirs` run outside the default dirs does not take over a copy stored from a live default skill (it would be foreign to every later default sync); such entries carry `reason: default_dir` in `foreign_skills`, and the hint advises a sync without `--dirs` for them.
- **Lifecycle lookup**: a SKILL.md that resolves into an ignored tree (`.archive/`, `.git/` ...) is not live, so an archive event no longer keeps rows for a skill reached through a link into `.archive/`, and an ingest event no longer indexes it. The lookup walks any depth and enters each real directory once (it stopped at six levels); discovery ends on link loops the same way.
- **Backfill**: once shutdown has begun, rows not confirmed written are logged instead of re-read and re-queued, so retries no longer use up the drain budget; the startup scan picks them up. When the driver cannot report per-row counts, the whole group is re-checked instead of dropped silently.
- **Test guard**: while the suite runs, `os.symlink` and `os.link` (and so `Path.symlink_to` / `Path.hardlink_to`, including Python 3.10's pathlib accessor) refuse a link whose location or target lies outside the temp dir, and every link when the temp dir contains the user's real home, overlaps its installed plugin dir (after resolving links) or lies inside `$HERMES_HOME/plugins`. The source scan parses the `_link_in_tmp` helper (a comment no longer satisfies it) and flags `os.link`, aliased calls, `hardlink_to`, `from os import symlink` and shell `ln -s` / `-fs` / `--symbolic` / `/bin/ln`.

### Changed
- **Version bumps**: `1.1.1.4` → `1.1.1.5` across `VERSION`, `plugin.yaml`, `pyproject.toml`, `__version__`, README/INSTALL badges, both `skills/*/SKILL.md` pins, `AGENTS.md` and `NEEDS.md`. Release notes rotated to `RELEASE_NOTES_1.1.1.5.md`.

## [1.1.1.4] - 2026-11-14

### Added
- `oracle_sync_skills`: `prune`, `prune_limit` and `dry_run`; `oracle_load_skill fetch`: `host`; `list` reports `copies`.
- `scripts/sync_skills.py`: `--prune`, `--list-local`, `--migrate-host-identity --listing <file>` (dry run unless `--apply`) and `--reverse-host-identity`.
- Embedding backfill with exponential backoff (2–300 s, 8 attempts) and a startup scan that re-queues rows with a NULL embedding (#7).
- Tests: `test_lifecycle_robustness.py`, `test_recall_path.py`, `test_tool_surface.py`, `test_skill_identity.py`, `test_skill_ownership.py`, `test_script_hygiene.py`.

### Fixed
- **Cross-host skill deletes (#5)**: skill rows are `skill:<host>:<name>:c<N>`; sync, lifecycle and prune delete only this host's id prefix (LIKE wildcards escaped), never other hosts' copies or the host-less shared corpus. Each changed skill is replaced in its own transaction; a failed skill is rolled back and reported (`status: "partial"`) while the rest continue. `get_host_id()` gives the same id with or without `HOSTNAME` in the environment.
- **Lifecycle hook (#6)**: work moved off the caller's thread (one writer per skill and table in the process), events during a write are coalesced instead of dropped, the skill roots follow the active profile's Hermes home, the event name is validated, and the stored identity is the parsed skill name (as in the bulk sync). The writer key is fixed at scheduling time, and coalesced events run on the provider they reached. Skills are found up to six levels below a root. A copy whose stored SKILL.md is live outside the scanned skill dirs is not replaced, and `archived` keeps any copy whose stored SKILL.md still exists (unless it is the archived symlink's target). Profiles sharing a host id are not fully isolated (#14).
- **Writes lost during an ONNX cooldown (#7)**: the row is written with a NULL embedding and backfilled; `shutdown()` drains the backfill within its budget. The backfill keeps only a row's latest text and writes a vector only while the row has none and still holds that text; rows the guard skips are re-read and re-queued with their stored text (bounded). Short text is bound as a string, longer text as a temporary CLOB released at the commit.
- **Pool lifecycle (#8)**: the old pool is retired on re-init and failed init; `shutdown()` does not close the pool while a worker is still busy.
- **Transactional insert (#9)**: savepoint per attempt inside a caller's transaction, no retry inside a broken transaction; `ORA-00060` is a data error.
- **Recall path (#10)**: fresh cursor per `ORA-51815` retry, CLOBs as strings in one round trip (`fetch_lobs=False`; the `DB_TYPE_LONG` output handlers are gone), sized `arraysize`/`prefetchrows`, ranking before the `top_k` cut, one hybrid text weight, timeouts classified by error code.
- **Tool surface (#11)**: `oracle_add` ≤ 100 facts × 32,000 chars, all-or-nothing; `oracle_get` limit enforced; search results capped at 4,000 chars with tags; `max_words` 10–1000; `skill:` rows read-only for generic tools (`oracle_delete` included); case-insensitive tags; memory mirror handles `replace`/`remove`.
- `oracle_load_skill`: `fetch` bounds rows per copy in SQL and keys copies by (host, path); `list` pages over distinct names (no repeated names, no short pages) and its filter is a literal substring; Oracle NUMBER metadata serialises.
- Bulk sync: `mode="full"` passed as a keyword was overridden by the positional default and ran a meta sync; another host's identical copy no longer counts as current; a meta-only copy is not current for a full sync; a copy is current only when it records the skill's present path; a same-host copy whose SKILL.md is live in another profile is never replaced (`skipped_foreign`).
- Stored SKILL.md paths are in-root (unresolved), so skill dirs symlinked into a root are archived and pruned correctly; `--list-local` listings carry both the in-root and the resolved path, and the migration matches legacy rows on either.

### Changed
- **Helper scripts (#12)** read `OCI_*` from the environment and load a KEY=VALUE file only with `--env-file`; nothing is read at import time.
- `pyproject.toml`: `license = "BUSL-1.1"` (SPDX) with `license-files`; build requires `setuptools>=77`. CLA allowlist names its bots instead of `*[bot]`.
- `scripts/sync_skills.py --migrate-host-tags` (path-prefix relabel) replaced by `--migrate-host-identity`.
- **Version bumps**: `1.1.1.3` → `1.1.1.4` across `VERSION`, `plugin.yaml`, `pyproject.toml`, `__version__`, README/INSTALL badges, both `skills/*/SKILL.md` pins, `AGENTS.md` and `NEEDS.md` (stale since 1.1.0.1; now covered by the version test).

### Notes
- No schema change. Single-current-notes convention maintained: this is `RELEASE_NOTES_1.1.1.4.md`; the v1.1.1.3 notes file was removed.
- Test suite: 450 passed / 3 skipped.

## [1.1.1.3] - 2026-11-14

### Added
- **Handoff rows**: `oracle_add` accepts an optional caller-chosen `memory_id` (upsert) and `tags` (stored lower-cased in `metadata.tags`); `oracle_update` can replace tags without re-embedding (`content` now optional); `oracle_search` filters by `tags` with `tag_match` `all`/`any` in every mode.
- **`oracle_get` tool**: exact read by `memory_id`(s) and/or tags, newest first, with `missing` ids reported; no embedding, so it works during an ONNX cooldown.
- System-prompt rule 10 (HANDOFFS) describing the `handoff:<project>:<topic>` convention.
- `tests/test_handoff.py`, timeout-consolidation tests, and a `tests.yml` CI workflow (Python 3.11/3.13, sandboxed `HOME`, version-drift check).

### Fixed
- **ONNX cooldown misclassification**: session/connection errors (`ORA-00028/01001/01002/03113/03114/03135`, `DPY-4005/4011/4024`), data/constraint errors (`ORA-00001`, `ORA-01400`, `ORA-12899`, …), and non-Oracle Python exceptions no longer start the 60 s process-wide ONNX cooldown.
- **Stale `call_timeout` on reused connections**: `_CallTimeoutPool` re-applies the default budget on every acquire (writes no longer inherit a read clamp → `DPY-4024`).
- **Poisoned pooled sessions**: connections that raised a session-level or non-Oracle error are dropped, not returned; search retries once on a broken session.
- **`on_skill_lifecycle` after provider shutdown**: the hook and background writer route to a live provider in the process instead of failing with `pool not initialized`; `shutdown()` no longer closes the pool under an active writer.
- **Unbounded pool wait**: pool uses `POOL_GETMODE_TIMEDWAIT` (20 s, `HERMES_ORACLE_POOL_WAIT_TIMEOUT_MS`) instead of blocking forever.
- **Search timeout env ignored**: `HERMES_ORACLE_SEARCH_TIMEOUT_MS` now drives the per-query clamp (default raised 13 s → 30 s); float values for `HERMES_ORACLE_INGEST_TIMEOUT` no longer crash `initialize()` / `scripts/sync_skills.py`.
- **Destructive tests**: the drift test no longer copies over `~/.hermes/plugins/oracle` and profile copies (now read-only, opt-in via `HERMES_ORACLE_DRIFT_DIR`); `tests/test_skill_bundles.py` (wrote into `~/.hermes/skills`) removed; the version test no longer deletes files.
- CLA workflow allowlist syntax (comma-separated).
- **Credential exfiltration via `oracle_chunk(file_path=...)`** (security): the tool read any file the model named and stored it in the shared database. Credential and system paths (`.env*`, `auth.json`, `.netrc`, `.ssh`/`.aws`/`.oci`/`.gnupg`/… directories, `*.pem`/`*.key`/wallets, `/etc`, `/proc`, `/root`, …) are refused after `resolve()`, so `..` and symlinks cannot bypass the check (macOS `/private/...` roots included; token stores such as `.config/gcloud`, `.config/gh`, `.envrc` and shell history too). The user's home and `HERMES_HOME` are exempt from the system-root check only, so root/service installs keep working (a home that is itself a system root such as `/etc` or `/var/lib` exempts nothing).
- **`oracle_delete` thread purge could wipe shared data**: `session_id="skills"` deleted the whole skill corpus and `"none"` unrelated rows. Reserved ids are refused, a purge never deletes `role='skill'` rows, and passing both `memory_id` and `session_id` is an error.
- **`oracle_sync_skills` argument handling**: a string `dirs` was iterated per character (scanning `/`); `force="false"` meant true. `dirs` accepts a string or list, refuses `/`, `$HOME`, system and credential dirs; `force` parses text; `mode`/`batch_size` validated.
- **Skill-lifecycle sync never ran in-process**: `from scripts.sync_skills import …` resolves to the host's own `scripts` package inside Hermes and failed silently. The module is now loaded by path and cached; importing it no longer reads the Hermes `.env`, re-execs, or edits `sys.path` (CLI only).
- **Archived skills never pruned**: lookups ignored hidden trees, so the `.archive/` copy no longer counts as "still present".
- **Connections reused after a call timeout**: a timed-out session (`DPY-4024`, `DPI-1067`, …) is dropped instead of returned to the pool; reusing it was the likely source of `ORA-01002` / `'NoneType' object is not iterable` on later searches. `_search` never retries a timeout.
- **Prefetch start race**: two concurrent prefetches could `start()` the same thread (`threads can only be started once`).
- **Failed `initialize()` looked ready**: on any init error the new pool is closed, `_initialized` cleared and the provider unregistered from the hook registry.
- **Busy `shutdown()` then re-`initialize()`** left background writes stopped/unregistered; the reuse path now re-arms the provider. **Skill-lifecycle upsert is atomic**: the DELETE of a skill's rows and the re-INSERT now commit in one transaction (`_insert_many(tx_conn=...)`, savepoint per attempt, embedding backfill queued after commit), so a failed or interrupted insert leaves the previous rows instead of removing the skill from every host. Lifecycle writers are started and recorded under one lock and refused once `shutdown()` has begun. `shutdown()` waits for in-flight skill-lifecycle writers like the other writers, gives a read-only prefetch 3 s, and if a connection is still borrowed closes the pool in the background once it returns (forced only after every call budget has expired) instead of leaking it or killing the call. A provider is registered for hooks only after schema setup succeeds.
- **`oracle_get`**: a string `memory_ids` is no longer split into characters (accepts one id, comma/space list, or JSON array); results cut off by `limit` report `truncated` instead of listing existing rows as `missing`; id lookups combined with tags default the limit to the id count.
- **Upsert race with a concurrent delete** returned `upserted: true` with nothing written; it now returns an error. `_update_row` and the `oracle_delete` purge drop broken sessions like other paths; the hook fallback picks the newest provider by registration order (WeakSet order is arbitrary).
- `scripts/sync_skills.py` parses `HERMES_ORACLE_INGEST_TIMEOUT` with the plugin's clamp (30–7200 s; bad/≤0 → 600 s) instead of crashing or disabling the timeout.

### Changed
- `created_at` projected in the search SQL; the secondary per-search query is gone (from PR #3).
- Pool idle timeout 5 s → 300 s (`HERMES_ORACLE_POOL_IDLE_TIMEOUT`); prefetch wait 0.5 s → 3 s (`HERMES_ORACLE_PREFETCH_WAIT`); pool fingerprint includes both.
- **Version bumps**: `1.1.1.2` → `1.1.1.3` across `VERSION`, `plugin.yaml`, `pyproject.toml`, `__init__.py.__version__`, README/INSTALL badges, and both `skills/*/SKILL.md` version pins.

### Notes
- No schema change. Single-current-notes convention maintained: this is `RELEASE_NOTES_1.1.1.3.md`; the v1.1.1.2 notes file was removed.
- Test suite: 323 passed / 3 skipped (new `tests/test_audit_fixes.py`).

## [1.1.1.2] - 2026-11-10

### Fixed
- **Search-timeout default corrected 10x**: `HERMES_ORACLE_SEARCH_TIMEOUT_MS` now defaults to `13000` (13 s), the true timeout budget used by the read-path fail-fast clamp (`_SEARCH_TIMEOUT_MS`, derived from the `13.0` s `HERMES_ORACLE_SEARCH_TIMEOUT`). The previous `1300` (1.3 s) value was an undetected 10x typo in `initialize()`'s env-read; it was inert in practice because `pool_call_timeout = max(search_ms, ingest_ms)` is always dominated by the 600 s ingest default, but the fingerprint and the pool's declared budget silently disagreed with the documented 13 s.
- **Single timeout read in `initialize()`**: the ingest/search timeout values are now computed from the environment at one point (below the pool-reuse check) and reused for both the pool fingerprint and the pool construction, eliminating the duplicated env-read block that previously recomputed the same three values inside the `try:` immediately before building `pool_kwargs`.

### Changed
- **`INSTALL.md`**: the ADB connection sample now uses the real TCP/TLS port (`1522`, the tcps port), replacing the generic `1521` placeholder.
- **README/INSTALL env tables**: now name the variable the code actually reads — `HERMES_ORACLE_SEARCH_TIMEOUT_MS` (default `13000`) — instead of the undocumented `HERMES_ORACLE_SEARCH_TIMEOUT`/`1300` the code never reads.
- **Version bumps**: `1.1.1` → `1.1.1.2` across `VERSION`, `plugin.yaml`, `pyproject.toml`, `__init__.py.__version__`, README/INSTALL badges, and both `skills/*/SKILL.md` version pins.

### Notes
- Single-current-notes convention maintained: this is `RELEASE_NOTES_1.1.1.2.md`; the v1.1.1 notes file was removed.
- Test suite unchanged at 241 passed / 2 skipped (the timeout tests already encode the corrected 13000 ms values).

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
