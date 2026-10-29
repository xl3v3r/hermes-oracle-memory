# Hermes Agent — Oracle 26ai Memory Provider

![Version](https://img.shields.io/badge/version-1.0.9.3.2-blue.svg)
![License](https://img.shields.io/badge/license-BSL--1.1-orange.svg)

> **CRITICALITY NOTICE**: v1.0.9.3.2 updates are CRITICAL to functionality. Standalone bulk import structural discovery (`_find_plugin_root`), fortified provider import fallback with path diagnostics, native skill library indexing engine (`oracle_sync_skills`), byte-safe UTF-8 clamping (`_safe_varchar_bytes`) to prevent `ORA-12899`, canonical root `VERSION` file, and zero drift across repository, installed plugin, and all Hermes Agent profile environments.

A high-performance external memory provider plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent), backed by **Oracle Autonomous AI Database 26ai**. It gives the agent durable, cross-session memory with fused **vector + full-text hybrid recall**, in-database ONNX embeddings, and background persistence — a scalable replacement for local SQLite / Mnemosyne-style memory.

## Install

> **CRITICALITY NOTICE**: v1.0.9.3.2 updates are CRITICAL to functionality. Standalone bulk import structural discovery (`_find_plugin_root`), fortified provider import fallback with path diagnostics, native skill library indexing engine (`oracle_sync_skills`), byte-safe UTF-8 clamping (`_safe_varchar_bytes`) to prevent `ORA-12899`, canonical root `VERSION` file, and zero drift across repository, installed plugin, and all Hermes Agent profile environments.

### Recommended — Hermes plugin manager

```bash
hermes plugins install xl3v3r/hermes-oracle-memory
hermes memory setup              # pick "oracle" from the list
```

### Manual — clone into the plugins directory

```bash
git clone https://github.com/xl3v3r/hermes-oracle-memory.git ~/.hermes/plugins/oracle
pip install "oracledb>=4.0.2"    # `oci` only needed for the OCI GenAI fallback
hermes config set memory.provider oracle
```

> Restart Hermes (or start a new session) after installing and selecting the provider.

### Verify

```bash
hermes memory status             # confirm "oracle" is active
python3 scripts/verify_provider.py
```

## Configure

Connection secrets go in `~/.hermes/.env` — see the [installation guide](INSTALL.md) for the full walkthrough. Minimum required:

```bash
OCI_DB_USER=ADMIN
OCI_DB_PASSWORD="..."
OCI_DB_DSN="(DESCRIPTION=(...))"        # Oracle Net descriptor or TNS alias
OCI_WALLET_LOCATION=~/.oci/wallet        # mTLS wallet dir (optional)
```

Embeddings are computed 100% in-house:

1. **In-database ONNX** (`HERMES_ORACLE_ONNX_MODEL=BGE_BASE_EN_V1_5`) — strictly in-database kernel execution; external fallback disabled.

## Tools

| Tool | Purpose |
|------|---------|
| `oracle_search` | hybrid / vector / text recall |
| `oracle_add`    | store a durable fact (embedded + indexed) |
| `oracle_update` | replace a fact by `memory_id` |
| `oracle_delete` | delete by `memory_id` |
| `oracle_chunk`  | split inline text or a disk file in-database |
| `oracle_sync_skills` | scan, parse, and index skill libraries (`~/.agents/skills`, `~/.hermes/skills`) into vector memory |

## Skills

- **`oracle-import`** — bulk-ingest conversation archives (ShareGPT / Claude / ChatGPT / JSON) and large documents into ADB without LLM rate limits or context inflation.

## What's new in v1.0.9.3.2

- **Criticality Notice**: v1.0.9.3.2 updates are CRITICAL to functionality. Restores standalone bulk conversation import via structural plugin root discovery (`_find_plugin_root`), fortifies provider dynamic import fallback with path diagnostics, purges historical release notes files, and guarantees zero drift across repository, installed plugin, and all Hermes Agent profile environments.
- **Defect 1 Fix: Structural Plugin Root Discovery (`_find_plugin_root`)**:
  - Replaced fragile 3-level static `.parent` traversal in `skills/oracle-import/scripts/import_conversations.py` with dynamic upward traversal `_find_plugin_root`.
  - Accurately locates the plugin root directory containing `plugin.yaml` and `__init__.py` whether executed from `skills/oracle-import/scripts/` or `scripts/`, resolving `FileNotFoundError: .../skills/__init__.py`.
- **Defect 2 Fix: Fortified Provider Dynamic Import Fallback**:
  - Fortified the provider fallback chain in `skills/oracle-import/scripts/import_conversations.py` to verify file existence and catch all exceptions during dynamic module spec creation and execution.
  - Replaced raw unhandled exceptions with actionable diagnostic logging specifying the exact failed file path and root cause.
- **Comprehensive Unit Testing**:
  - Added dedicated test suite `TestOracleImportScript` in `tests/test_oracle_import.py` validating structural discovery and error handling branches.
- **Repository Hygiene & Clean Release Documentation**:
  - Purged legacy historical release notes files, maintaining clean, dedicated release documentation (`RELEASE_NOTES_1.0.9.3.2.md` and `CHANGELOG.md`).
  - Zero modifications to Hermes Agent core repository files.


## Contributing

Pull requests welcome. First-time contributors sign the [CLA](CLA.md) by commenting on the PR:

```text
I have read the CLA Document and I hereby sign the CLA
```

## License

[Business Source License 1.1](LICENSE) — free for personal, research, and organizations under $1M annual revenue; converts to Apache-2.0 on 2030-10-11.
