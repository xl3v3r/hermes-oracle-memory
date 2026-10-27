# Developer & Agent Guidelines (AGENTS.md)

Welcome! This file provides context, coding conventions, testing procedures, and architectural guidelines for autonomous agents and developers contributing to `hermes-oracle-memory`.

---

## 1. Project Overview

`hermes-oracle-memory` is a high-performance external memory provider plugin for **Hermes Agent**. It integrates with **Oracle Autonomous AI Database 26ai (ADB)** to provide:
- Hybrid memory recall combining `VECTOR(768, FLOAT32)` IVF neighbor-partition search and Oracle Text `CONTAINS` index.
- In-database ONNX embeddings (`BGE_BASE_EN_V1_5`) strictly in-database via ONNX runtime (no external fallback).
- In-database text chunking (`DBMS_VECTOR.UTL_TO_CHUNKS`).
- Tool execution for Hermes memory recall (`oracle_search`, `oracle_add`, `oracle_update`, `oracle_delete`, `oracle_chunk`).

---

## 2. Directory Structure

```text
├── AGENTS.md               # Guidelines and instructions for AI agents and developers
├── INSTALL.md              # Detailed installation and configuration guide
├── LICENSE                 # Business Source License 1.1
├── MANIFEST.in             # Source distribution manifest
├── README.md               # Project documentation and usage guide
├── RELEASE_NOTES_1.0.9.2.md # Release notes for v1.0.9.2
├── RELEASE_NOTES_1.0.9.3.md # Release notes for v1.0.9.3
├── RELEASE_NOTES_1.0.9.3.1.md # Release notes for v1.0.9.3.1
├── VERSION                 # Canonical project version file (1.0.9.3.1)
├── __init__.py             # Core plugin implementation and OracleMemoryProvider class
├── plugin.yaml             # Hermes plugin manifest and metadata
├── pyproject.toml          # Python build, dependencies, and packaging configuration
├── scripts/                # Utility scripts (verification, backfill, ONNX setup, skill sync)
│   ├── audit_embeddings.py
│   ├── backfill_embeddings.py
│   ├── import_conversations_ordered.py
│   ├── reembed_embeddings.py
│   ├── run_tests.py
│   ├── setup_onnx_model.py
│   ├── sync_skills.py
│   └── verify_provider.py
├── skills/                 # Hermes agent skills
│   └── oracle-import/      # Bulk conversation and archive ingestion skill
└── tests/                  # Unit test suite (160+ tests)
    ├── conftest.py         # Test configuration & mocks for Hermes host and DB drivers
    ├── test_format_prefetch.py # Prefetch formatting tests
    ├── test_init.py        # Gateway turn detection and config schema tests
    ├── test_oracle.py      # Core provider unit tests, edge cases, is_available tests
    ├── test_oracle_import.py# Conversation import and chunking tests
    ├── test_plugin.py      # Tool schema and unavailable_reason tests
    ├── test_reembed.py     # Re-embedding and vector upgrade tests
    └── test_sync_skills.py # Native skill library scanner and frontmatter parser tests
```

---

## 3. Environment & Mocking Conventions

Hermes plugins run dynamically inside the Hermes agent runtime. When developing or running automated CI/CD tests:
- Hermes host modules (`agent`, `tools`) are dynamically provided by the runtime.
- In test environments, `tests/conftest.py` provides lightweight mocks for `agent`, `tools`, `oracledb`, and `oci`.
- Always ensure new tests rely on `tests.conftest` or standard `unittest.mock` rather than requiring a live Oracle database or network connection.

---

## 4. Running Tests

Unit tests can be run using either Python's standard `unittest` or `pytest`:

```bash
# Using unittest
python3 -m unittest discover tests

# Using pytest
pytest
```

All tests in `tests/` must pass cleanly without network access or active database credentials.

---

## 5. Security & Best Practices

1. **SQL Sanitization**: All table and model names interpolated into SQL queries must be sanitized (`re.sub(r"[^a-zA-Z0-9_]", "", ...)`). Apply `# nosec B608` comments to suppress false-positive Bandit warnings on sanitized table variables.
2. **Batched Execution**: Always use `cursor.executemany` for batch database updates (e.g. embedding backfill) to avoid N+1 query overhead.
3. **Type Safety & Defensive Validation**: Ensure lifecycle methods (`initialize`, `sync_turn`, `prefetch`, `on_memory_write`, `on_session_end`) guard against non-string or `MagicMock` inputs.

