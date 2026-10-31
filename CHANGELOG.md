# Changelog — `hermes-oracle-memory`

All notable changes to this project are documented in this file.

## [1.0.9.4.2] - 2026-10-31

### Fixed & Fortified
- **Skill Ingest Retrieval Visibility (Defect 1)**:
  - Replaced over-greedy `[skill%` SQL filter with single-source `_telemetry_sql()` generator derived from `_TELEMETRY_PREFIXES`.
  - Accurately filters only lifecycle telemetry (`[skill\_usage]%`, `[skill\_vectorization%`) with SQL LIKE wildcard escaping, unblocking full candidate recall of `[skill_ingest]` indexed skills across hybrid, vector, and text search modes.
- **In-Process Skill Sync Dataclass Introspection (Defect 2)**:
  - Extracted `_load_module_from_path` helper ensuring dynamic module imports register the module object in `sys.modules` prior to bytecode execution.
  - Eliminates CPython `@dataclass` introspection failure (`'NoneType' object has no attribute '__dict__'`) when calling `oracle_sync_skills` in-process.
- **Generic Profile Synchronization & Zero-Drift**:
  - Dynamically discovers all Hermes Agent profile plugin directories under `Path.home() / ".hermes" / "profiles"` without hardcoding specific profile names.
  - Generically syncs files to profile directories and validates profile symlinks against `~/.hermes/plugins/oracle`.
  - Automatically purges obsolete historical release notes from sync destinations.

### Added
- Unit tests verifying `_telemetry_sql` wildcard escaping and telemetry-only exclusion in `tests/test_oracle.py`.
- Regression test for `_load_module_from_path` validating CPython dataclass resolution in `tests/test_plugin.py`.

### Changed
- Bumped canonical version to `1.0.9.4.2` across `VERSION`, `__init__.py`, `plugin.yaml`, `pyproject.toml`, `README.md`, `CHANGELOG.md`, `INSTALL.md`, and `skills/oracle-import/SKILL.md`.
- Enforced 100% zero drift between `/home/rocky/build/hermes-oracle-memory`, `~/.hermes/plugins/oracle`, and all generic profile plugin directories.
- Purged previous version release notes (`RELEASE_NOTES_1.0.9.3.2.md`, `RELEASE_NOTES_1.0.9.4.md`, `RELEASE_NOTES_1.0.9.4.1.md`) per single-release documentation policy.

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
- Enforced 100% zero drift between `/home/rocky/build/hermes-oracle-memory`, `~/.hermes/plugins/oracle`, and all profile environments.

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
