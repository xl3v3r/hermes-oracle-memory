# Release Notes — v1.1.2

## Version 1.1.2 (2026-11-18) — Per-user memory scoping

### Summary

`hermes-oracle-memory` v1.1.2 adds per-user memory scoping isolation between global and user sessions, enabling safe deployments with Open WebUI and other multi-user frontends.

### Added

- **`scope_id` column** (`VARCHAR2(300) DEFAULT 'global' NOT NULL`) on the Oracle memory table, providing scope isolation
- **`HERMES_ORACLE_SCOPE_MODE`** environment variable (`global`, `user`, `auto`, `required`) to control scope behavior
- **Gateway session key derivation**: scope_id can be derived from `HERMES_ORACLE_GATEWAY_SESSION_KEY` for per-user isolation in multi-tenant deployments
- **GSK validation** (`_verify_caller_gsk`): validates caller GSK against bound scope_id — global scope accepts any valid GSK, non-global requires GSK derivation from scope_id
- **Skill sync gating**: `oracle_sync_skills` is blocked when `scope_id != "global"`, preventing accidental skill sync in user-scoped sessions
- **Scope-aware search/get/update/delete**: All memory operations now filter by `scope_id`, ensuring users only see their own rows

### Changed

- **Scope isolation** (issue #16): No more cross-user memory leakage — every read/write/delete is bounded by `scope_id`
- **Derivation logic** (issue #17): `HERMES_ORACLE_SCOPE_MODE` with `auto`/`required` modes and `gateway_session_key` fallback
- **`_row_filters`**: Added `AND scope_id = :scope` predicate to all SQL operations for consistent isolation
- **`handle_tool_call`**: Added caller GSK extraction and validation before any tool operation

### Fixed

- **Issue #16**: No isolation between agents, profiles or users — now every read/write path is scope-bounded
- **Issue #17**: Per-user memory scoping for Open WebUI and other multi-user frontends (`X-Hermes-Session-Key`)
- **Issue #25**: Release wheel now includes `scripts/` directory ( `include-package-data = true` in pyproject.toml)
- **Issue #18**: Text index DDL failure handling improved — errors are now properly recorded and surfaced via `_verify_schema`
- **Issue #14**: Skill identity follow-ups resolved with shared host id and `skipped_foreign` override

### Deprecated

- None

### Removed

- None

