# Changelog — `hermes-oracle-memory`

All notable changes to this project are documented in this file.

## [1.0.9.3.2] - 2026-10-29

### Fixed
- **Defect 1 (`plugin_root` structural discovery)**: Replaced static 3-level parent traversal with `_find_plugin_root` upward directory scanning for `plugin.yaml` and `__init__.py` in `skills/oracle-import/scripts/import_conversations.py`. Restores bulk conversation and archive migration without `FileNotFoundError`.
- **Defect 2 (Provider import fallback chain)**: Fortified provider dynamic import fallback chain in `import_conversations.py` to catch all exceptions, verify file existence, and emit diagnostic path logging.

### Added
- Unit test suite `TestOracleImportScript` in `tests/test_oracle_import.py` for structural discovery and fallback error branches.

### Changed
- Bumped version to `1.0.9.3.2` across `__init__.py`, `VERSION`, `plugin.yaml`, `pyproject.toml`, `skills/oracle-import/SKILL.md`, `README.md`, `INSTALL.md`, and test suites.
- Purged previous historical release notes files prior to v1.0.9.3.2 per release policy.
