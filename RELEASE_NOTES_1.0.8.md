# Release Notes — Hermes Agent Oracle 26ai Memory Provider v1.0.8

> [!IMPORTANT]
> **CRITICALITY NOTICE**: v1.0.8 updates are CRITICAL to functionality. Hardened serial execution, prompt injection defense, thread purging, and search scoping require this version.

### What's New in v1.0.8

#### ⚡ Mandatory Serial Execution & Auto-DOP Hardening
- **Universal `/*+ NO_PARALLEL */` Hints**: Injected optimizer hints across all SQL builders (vector search, Oracle Text full-text search, fused hybrid search) and all write operations (`_insert`, `_insert_many`, ONNX batch embed `SELECT`, embed backfill `UPDATE`, `oracle_update`, and `oracle_delete`). Prevents Exadata auto-DOP parallel execution stalls, parallel query contention, and ORA-04036 PGA limit exceptions under high concurrency.
- **Session-Level Serial Callback**: Enforced on every database connection acquired from `SessionPool` via `session_callback`:
  - `ALTER SESSION SET parallel_degree_policy = MANUAL`
  - `ALTER SESSION DISABLE PARALLEL QUERY`
  - `ALTER SESSION DISABLE PARALLEL DML`
- **Maintenance & Migration Script Hardening**: `scripts/audit_embeddings.py`, `scripts/backfill_embeddings.py`, and `scripts/reembed_embeddings.py` enforce session-level serial mode and explicit table-aliased optimizer hints (`UPDATE /*+ NO_PARALLEL(m) */`, `MERGE /*+ NO_PARALLEL */`). `reembed_embeddings.py` adds session-scoped parallel slave validation (`assert_no_px`).

#### 🛡️ Prompt Injection Defense & Prefetch XML Isolation
- **XML Tag Boundary Isolation**: Prefetched memories injected into agent prompts are now wrapped in explicit `<retrieved_memory id="..." target="..." score="...">...</retrieved_memory>` isolation tags to cleanly separate system instructions from stored memories.
- **Control Character Stripping**: Strips ASCII control characters (`[\x00-\x08\x0b-\x1f\x7f]`) from prefetched content to prevent terminal injection, prompt truncation, or parser disruption.
- **Attribute Escaping**: Memory metadata attributes (`id`, `target`, `score`) are escaped via `html.escape(..., quote=True)` to guard against XML injection.

#### 🗑️ Cascading Thread Purge in `oracle_delete`
- **Thread & Session Purging**: `oracle_delete` now supports `session_id` in addition to `memory_id`. Specifying `session_id` cascades a bulk deletion across all memory turns and chunks associated with the specified conversation session.
- **Flexible Schema**: Removed `required: ["memory_id"]` from `oracle_delete` schema so agents can delete by individual memory ID or purge an entire conversation thread by session ID.

#### 🔒 Schema Policy Enforcement (`HERMES_ORACLE_SCHEMA_POLICY=REQUIRE_EXISTING`)
- **Restricted Database Environments**: Supports `HERMES_ORACLE_SCHEMA_POLICY=REQUIRE_EXISTING` to bypass runtime DDL table creation (`_ensure_schema()`) and verify existing schema objects directly (`_verify_schema()`). Enables zero-privilege operation in restricted production databases where application users lack `CREATE TABLE` / `CREATE INDEX` rights.

#### 🎯 Search Scoping Filters (`session_id`, `agent_id`)
- **Scoped Hybrid Recall**: Added optional `session_id` and `agent_id` parameters to `oracle_search` schema and query builders.
- **Optimizer Plan Stabilization**: Filters are bound safely via bind variables (`:sid`, `:aid`) while preserving `{self._JUNK}` query suffixes for plan cache stabilization.

#### 🔑 Credential Protection
- **Password Redaction**: `_init_error` sanitizes database connection passwords, replacing plain-text credentials with `******` in logged exceptions and error outputs.

#### 💼 ONNX Setup Tooling Improvements
- **Automatic Wallet Discovery**: `scripts/setup_onnx_model.py` automatically resolves client credential wallets from `OCI_WALLET_LOCATION`, `~/.oci/wallet`, or `~/.hermes/oracle_wallet`.
- **Encrypted Wallet Support**: Added `OCI_WALLET_PASSWORD` handling and enforced serial execution during model setup.
