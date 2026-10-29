# Release Notes — Hermes Agent Oracle 26ai Memory Provider v1.0.9.3.2

> [!IMPORTANT]
> **CRITICALITY NOTICE**: v1.0.9.3.2 updates are CRITICAL to functionality. Restores standalone bulk conversation import via structural plugin root discovery (`_find_plugin_root`), fortifies provider dynamic import fallback with path diagnostics, purges historical release notes files, and guarantees zero drift across repository, installed plugin, and all Hermes Agent profile environments.

### What's New in v1.0.9.3.2

#### 🛠️ Defect 1 Fix: Structural Plugin Root Discovery (`_find_plugin_root`)
- **Root Cause Resolved**: The bulk import script previously attempted a static 3-level `.parent` traversal, resolving to `<plugin>/skills` instead of `<plugin>` and causing `FileNotFoundError: .../skills/__init__.py`.
- **Structural Upward Discovery**: Implemented `_find_plugin_root()` traversing directory parents to identify the directory holding `plugin.yaml` and `__init__.py`.
- **Location Resilience**: The migration script can now execute from `skills/oracle-import/scripts/`, `scripts/`, or arbitrary subdirectories without relative path fragility.

#### 🛡️ Defect 2 Fix: Fortified Provider Dynamic Import Fallback
- **Robust Exception Handling**: Fortified the fallback chain in `skills/oracle-import/scripts/import_conversations.py` to catch `(ImportError, FileNotFoundError, Exception)`.
- **Diagnostic Logging**: Evaluates candidate paths and emits explicit logging if `__init__.py` is missing or module spec creation fails, replacing raw unhandled tracebacks with actionable diagnostics.

#### 🧪 Comprehensive Unit Testing
- **Structural Discovery Unit Tests**: Added `TestOracleImportScript` in `tests/test_oracle_import.py` verifying `_find_plugin_root` across skill directories, scripts subdirectories, and fallback cases.
- **Provider Load Diagnostic Tests**: Verified graceful failure handling when the plugin init file is missing or invalid.

#### 🧹 Repository Hygiene & Historical Release Notes Purge
- **Clean Release Documentation**: Purged all legacy historical release notes files prior to v1.0.9.3.2 (`RELEASE_NOTES_1.0.9.2.md`, `RELEASE_NOTES_1.0.9.3.md`, `RELEASE_NOTES_1.0.9.3.1.md`), establishing a single source of truth for v1.0.9.3.2 (`RELEASE_NOTES_1.0.9.3.2.md` and `CHANGELOG.md`).
- **Core File Isolation**: Zero modifications made to Hermes Agent core repository files (`~/.hermes/hermes-agent`).
- **Zero Drift**: Full byte-for-byte synchronization across the repository and the local Hermes plugin environment.
