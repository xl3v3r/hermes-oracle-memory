# Hermes Agent — Oracle 26ai Memory Provider

![Version](https://img.shields.io/badge/version-1.0.9-blue.svg)
![License](https://img.shields.io/badge/license-BSL--1.1-orange.svg)

> **CRITICALITY NOTICE**: v1.0.9 updates are CRITICAL to functionality. Automated on_skill_lifecycle memory hook, updated 9 Oracle 26ai memory rules, local memory deactivation, and zero-drift deployment require this version.

A high-performance external memory provider plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent), backed by **Oracle Autonomous AI Database 26ai**. It gives the agent durable, cross-session memory with fused **vector + full-text hybrid recall**, in-database ONNX embeddings, and background persistence — a scalable replacement for local SQLite / Mnemosyne-style memory.

## Install

> **CRITICALITY NOTICE**: v1.0.9 updates are CRITICAL to functionality. Automated on_skill_lifecycle memory hook, updated 9 Oracle 26ai memory rules, local memory deactivation, and zero-drift deployment require this version.

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

## Skills

- **`oracle-import`** — bulk-ingest conversation archives (ShareGPT / Claude / ChatGPT / JSON) and large documents into ADB without LLM rate limits or context inflation.

## What's new in v1.0.9

- **Criticality Notice**: v1.0.9 updates are CRITICAL to functionality. Implements Option B automated `on_skill_lifecycle` observer hook recording skill activations directly into Oracle 26ai ADB persistent memory as vectorized facts with zero external API transit.
- **Automated Skill Lifecycle Observer Hook (Option B)**:
  - In `register(ctx)`: registers an observer hook on `on_skill_lifecycle` via `ctx.register_hook("on_skill_lifecycle", callback)`.
  - Captures skill lifecycle actions (`action in ("used", "loaded")`) and records compact facts (<200 chars satisfying Rule 2 and avoiding 280-char prefetch truncation) stamped with Rule 8 bracketed tags:
    `[skill_usage] [skill:<skill_name>] [action:<action>] [ts:<ISO8601>] [session:<session_id>] Skill '<skill_name>' activated.`
  - Asynchronously enqueues writes to Oracle ADB memory via `_queue_background_insert` for non-blocking, fail-safe background persistence.
  - Zero external API transit: 100% in-database ONNX embedding generation strictly within Oracle Autonomous AI Database 26ai.

## What's new in v1.0.8.2

- **Criticality Notice**: v1.0.8.2 updates are CRITICAL to functionality. Eliminates external OpenRouter fallback for strict 100% in-house database ONNX embeddings, tunes connection pool limits (`min: 2, max: 6, call_timeout: 30000`), fixes hyphen minus syntax collisions in Oracle Text queries, and registers `oracle_search` as parallel-safe.
- **100% In-House Data Retention (No External Fallback)**: Completely removed the external OpenRouter embedding fallback. Vector embeddings are generated strictly in-database via ONNX `VECTOR_EMBEDDING`, ensuring confidential agent memory never leaves the customer database enclave.
- **Session Pool Concurrency Tuning**: Adjusted pool parameters to `min: 2, max: 6` with `call_timeout: 30000` ms to eliminate pool contention and connection starvation during concurrent tool calls and turn synchronization.
- **Oracle Text Syntax Resilience**: Sanitized hyphen/minus characters in `_CONTAINS_CLEAN_RE` so queries with hyphenated terms (e.g. `top-secret`) do not trigger `DRG-50901: text query syntax error`.
- **Parallel Dispatch Safety**: Registered `oracle_search` under Hermes agent `_PARALLEL_SAFE_TOOLS` for concurrency-safe execution alongside parallel tool calling.

## What's new in v1.0.8.1

- **Criticality Notice**: v1.0.8.1 updates are CRITICAL to functionality. Updated 9 Oracle 26ai memory rules, local memory deactivation, and zero-drift deployment require this version.
- **Updated 9 Oracle 26ai Memory Rules**: System prompt enforces strict 9 rules covering Oracle-first search, atomic fact sink, memory_id lifecycle, storage tiers, Oracle-only store (no local MEMORY.md), Oracle unavailability behavior, internal write-ahead logging (`wal_*`), session scoping tags, and pre-write validation.
- **Local Memory Deactivation & Rule 6 Fallback**: Aligned provider fallback prompt with Rule 6 to prohibit local memory; Oracle ADB 26ai serves as the sole system of record.

## What's new in v1.0.8

- **Criticality Notice**: v1.0.8 updates are CRITICAL to functionality. Hardened serial execution, prompt injection defense, thread purging, and search scoping require this version.
- **Mandatory Serial Execution & Auto-DOP Hardening**:
  - Injected `/*+ NO_PARALLEL */` optimizer hints across all query builders (vector search, full-text search, hybrid search) and DML operations (`_insert`, `_insert_many`, ONNX batch embed `SELECT`, backfill `UPDATE`, `oracle_update`, `oracle_delete`).
  - Session callback on every connection pool acquire enforces serial mode via `parallel_degree_policy = MANUAL`, `DISABLE PARALLEL QUERY`, and `DISABLE PARALLEL DML`.
  - Migration and re-embedding utilities hardened with session serial enforcement and explicit table-aliased hints (`UPDATE /*+ NO_PARALLEL(m) */`, `MERGE /*+ NO_PARALLEL */`).
- **Prompt Injection Defense & Prefetch XML Isolation**:
  - Prefetched memory items wrapped in explicit `<retrieved_memory id="..." target="..." score="...">` isolation tags.
  - Strips ASCII control characters (`[\x00-\x08\x0b-\x1f\x7f]`) and escapes metadata attributes to prevent XML injection and terminal disruption.
- **Cascading Thread Purge in `oracle_delete`**:
  - `oracle_delete` accepts `session_id` to purge all memory turns and chunks associated with an entire conversation thread.
  - Relaxed tool schema requirements (`required: ["memory_id"]` removed) to support both item and thread deletion.
- **Schema Policy Enforcement (`HERMES_ORACLE_SCHEMA_POLICY=REQUIRE_EXISTING`)**:
  - Allows skipping runtime DDL creation (`_ensure_schema()`) in locked-down or least-privilege database environments.
- **Search Scoping Filters**:
  - `oracle_search` accepts optional `session_id` and `agent_id` filters while preserving query plan stability.
- **Credential Protection**:
  - Database connection password redacted from error output and initialization failure diagnostics.
- **ONNX Setup Wallet Resolution**:
  - Automatic wallet location resolution and encrypted wallet support (`OCI_WALLET_PASSWORD`) in `scripts/setup_onnx_model.py`.

## What's new in v1.0.7.1

- **Criticality Notice**: v1.0.7.1 updates are CRITICAL to functionality. Oracle-first memory behavior and batch writes require this version.
- **Oracle-First System Prompt & Dynamic Enforcement**:
  - Injected header: `# Oracle 26ai Memory (Primary Store)`.
  - Rule 1 — ORACLE-FIRST SEARCH: `oracle_search` must always be consulted first before answering queries that depend on prior context.
  - Rule 2 — ORACLE-FIRST FACT SINK: `oracle_add` is the primary sink for all lasting facts and user details (unlimited storage).
  - Rule 3 — ANTI-CONFLATION & NO CROSS-STORE DELETION: Local `MEMORY.md` is strictly limited to 1,600 chars for bootstrap notes only. Never pass `chk_*` or `fact_*` IDs to the local memory tool.
  - Rule 4 — BATCH & DOCUMENT STORAGE: Atomic facts via `oracle_add`, long documents/logs via `oracle_chunk`.
- **Batch Fact Ingestion**: `oracle_add` accepts `facts: list[str] | str` as well as `content: list[str] | str` to persist multiple atomic facts in a single call.
- **Tool Schema Anti-Conflation**: Schemas across `oracle_search`, `oracle_add`, `oracle_update`, and `oracle_delete` explicitly instruct against using local memory tools for Oracle entries.
- **ADB Service Tier Migration & Pool Optimization**: Automatic case-insensitive normalization of `_high` DSN service tiers to `_low`. SessionPool defaults configured to `min=1, max=3, increment=1, threaded=True, getmode=oracledb.POOL_GETMODE_WAIT, timeout=5, wait_timeout=5`.
- **OS-Agnostic Drop-In Design & Zero Drift**: Zero hardcoded OS or user paths; byte-for-byte parity between repository and installed plugin directory.

## What's new in v1.0.7

- Ordered conversation chunking: `_chunk_and_store` chunks each turn independently and stamps `turn_index`, `chunk_index`, `turn_id`, `conv_ref` so history recalls in order (`ORDER BY turn_index, chunk_index` per `conv_ref`). New `scripts/import_conversations_ordered.py` imports ChatGPT/ShareGPT archives in native message order.
- Embedding provenance: `embedding_model` / `embedding_updated_at` stamped on every write path; external-embed kill-switch enforced on `_embed_external_tagged`.
- Merged upstream v1.0.6.1 (CLOB LOB normalization fix in `_fetch_rows`, ONNX batching test).

## What's new in v1.0.6.1

- Fix recall returning `LOB` objects for CLOB content: `_fetch_rows` now normalizes CLOB values to `str`, fixing a `TypeError: 'LOB' object is not subscriptable` crash in hybrid rerank, broken prefetch formatting, and un-serializable `oracle_search` results under `oracledb` thin mode.

## What's new in v1.0.6

- Batched in-database ONNX embeddings via a single `UNION ALL` query (no N+1 round-trips).
- Single background writer thread replaces thread-per-turn persistence.
- `DBMS_VECTOR.RERANK` is opt-in (`HERMES_ORACLE_DISABLE_RERANK=1`) and capped; prefetch skips rerank.
- Embed backfill re-queues failed batches instead of silently dropping rows; `shutdown()` drains pending writes before closing the pool.
- Removed the `DB_TYPE_LONG` output handler that truncated CLOB content over 32 KB.
- Sanitized the ONNX model identifier in the backfill script; added an external-embed kill-switch (`HERMES_ORACLE_DISABLE_EXTERNAL_EMBED=1`); `.gitignore` covers wallets/keys; `oracle_chunk` caps input at 64 MB; CLA action pinned by SHA and scoped.

## Contributing

Pull requests welcome. First-time contributors sign the [CLA](CLA.md) by commenting on the PR:

```text
I have read the CLA Document and I hereby sign the CLA
```

## License

[Business Source License 1.1](LICENSE) — free for personal, research, and organizations under $1M annual revenue; converts to Apache-2.0 on 2030-10-11.
