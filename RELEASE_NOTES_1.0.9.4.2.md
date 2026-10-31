# Release Notes — Hermes Agent Oracle 26ai Memory Provider v1.0.9.4.2

> **CRITICALITY NOTICE**: v1.0.9.4.2 delivers skill-path remediation across retrieval and ingest, generic multi-profile synchronization, and strict release hygiene. Unblocks skill index retrieval in `oracle_search` by eliminating an over-greedy candidate filter, restores in-process `oracle_sync_skills` execution by fixing CPython dataclass module registration, and provides dynamic discovery across all Hermes Agent profile environments with 100% zero drift.

### What's New in v1.0.9.4.2

#### 🔍 Skill Retrieval Unblocked — Defect 1 Fix (`_telemetry_sql`)

- **Root Cause Remediated**: The previous candidate filtering predicate (`_JUNK`) appended `AND NVL(content_txt, 'x') NOT LIKE '[skill%'` to all hybrid, vector, and text SQL queries. While intended to filter out lifecycle telemetry (`[skill_usage]`, `[skill_vectorization*]`), this greedy pattern inadvertently matched and eliminated all indexed skill records (`[skill_ingest] ...`), rendering them completely invisible to `oracle_search`.
- **Single-Source SQL Filter Helper**: Introduced `_telemetry_sql(col)` directly derived from `_TELEMETRY_PREFIXES`. Escapes SQL wildcard characters (`_` -> `\_`, `%` -> `\%`) and constructs targeted negative `LIKE` predicates with explicit `ESCAPE '\'` syntax.
- **Search Candidate Visibility Restored**: `[skill_ingest]` records are now fully retrievable across all recall modes (`oracle_search(..., mode="hybrid"|"vector"|"text")`), while lifecycle telemetry remains cleanly excluded from candidate pools.

#### 🛠️ In-Process Skill Sync Fixed — Defect 2 Fix (`_load_module_from_path`)

- **Root Cause Remediated**: Dynamic script execution via `importlib.util.module_from_spec` failed during in-process invocations of `oracle_sync_skills` because the module was not registered in `sys.modules` prior to executing bytecode with `exec_module()`. CPython's `@dataclass` decorator introspects `sys.modules.get(cls.__module__).__dict__`; because the module was unregistered, this evaluated to `None.__dict__`, raising `AttributeError: 'NoneType' object has no attribute '__dict__'`.
- **Pre-Registration Module Loader**: Added `_load_module_from_path(name, path)` helper that pre-populates `sys.modules[name] = mod` before `spec.loader.exec_module(mod)` runs, with clean rollback on exception.
- **Flawless In-Process Tool Execution**: Restores in-process `oracle_sync_skills(mode="meta"|"full")` execution without subprocess spawning or import failure.

#### 🌐 Generic Multi-Profile Synchronization

- **Dynamic Profile Discovery**: Replaced hardcoded bot profile names with dynamic directory scanning under `Path.home() / ".hermes" / "profiles"`.
- **Generic Installation & Link Management**: Discovers all active Hermes Agent profiles, automatically establishing plugin symlinks to `~/.hermes/plugins/oracle` where applicable and synchronizing standalone profile plugin installations.
- **Zero Drift Guarantee**: Automated test suite validates 100% byte-for-byte fidelity across canonical repository, installed plugin, and all profile environments.

#### 🧹 Release Documentation Hygiene

- **Single Release Notes Policy**: Purged historical release notes (`RELEASE_NOTES_1.0.9.3.2.md`, `RELEASE_NOTES_1.0.9.4.md`, `RELEASE_NOTES_1.0.9.4.1.md`) from git tracking and sync targets, ensuring only the current version release notes file (`RELEASE_NOTES_1.0.9.4.2.md`) is maintained.
- **Information Protection**: Fully sanitized all internal bot identifiers across documentation, changelogs, tests, and release notes.

#### 🧪 Expanded Test Coverage

- Added unit tests for `_telemetry_sql` SQL fragment escaping and filtering.
- Replaced stale junk filter assertions with tests verifying telemetry filtering and skill ingest preservation.
- Added regression test for `_load_module_from_path` validating CPython dataclass resolution.
- Expanded zero-drift verification with generic profile discovery.
