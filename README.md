# Hermes Agent — Oracle 26ai Memory Provider

![Version](https://img.shields.io/badge/version-1.1.1-blue.svg)
![License](https://img.shields.io/badge/license-BSL--1.1-orange.svg)

> **CRITICALITY NOTICE**: Shared-corpus read path (oracle_load_skill), live sync on skill create/update (on_skill_lifecycle ingest), zombie-safe host-scoped pruning, idempotent init, separated ingest timeout.

A high-performance external memory provider plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent), backed by **Oracle Autonomous AI Database 26ai**. It gives the agent durable, cross-session memory with fused **vector + full-text hybrid recall**, in-database ONNX embeddings, background persistence, and a **shared fleet-wide skill corpus**.

## Install

> **CRITICALITY NOTICE**: Shared-corpus read path (oracle_load_skill), live sync on skill create/update (on_skill_lifecycle ingest), zombie-safe host-scoped pruning, idempotent init, separated ingest timeout.

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
python3 tests/verify_provider.py
```

## Configure

Connection secrets go in `~/.hermes/.env` — see the [installation guide](INSTALL.md) for the full walkthrough. Minimum required:

```bash
OCI_DB_USER="<your-db-user>"
OCI_DB_PASSWORD="..."
OCI_DB_DSN="(DESCRIPTION=(...))"        # Oracle Net descriptor or TNS alias
OCI_WALLET_LOCATION=~/.oci/wallet        # mTLS wallet dir (optional)
```

Embeddings are computed 100% in-house:

1. **In-database ONNX** (`HERMES_ORACLE_ONNX_MODEL=BGE_BASE_EN_V1_5`) — strictly in-database kernel execution; external fallback disabled.

### Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `OCI_DB_USER` | `<your-db-user>` | Database user name |
| `OCI_DB_PASSWORD` | - | Database user password |
| `OCI_DB_DSN` | - | Connection string / TNS descriptor |
| `OCI_WALLET_LOCATION` | - | mTLS client wallet directory (`~/.oci/wallet`) |
| `HERMES_HOST_ID` | System user | Unique host tag for shared-corpus skill entries |
| `HERMES_ORACLE_TABLE` | `HERMES_AGENT_MEMORY` | Memory table name |
| `HERMES_ORACLE_ONNX_MODEL` | `BGE_BASE_EN_V1_5` | In-database ONNX model name |
| `HERMES_ORACLE_SKILL_FETCH_MAX_CHUNKS` | `400` | Safety cap for chunk reassembly during `oracle_load_skill(fetch)` |
| `HERMES_ORACLE_INGEST_TIMEOUT` | `600` | Timeout in seconds for batch skill syncing and ingestion operations |
| `HERMES_ORACLE_SEARCH_TIMEOUT_MS` | `1300` | Timeout in milliseconds for interactive recall queries |

## Tools

| Tool | Purpose |
|---|---|
| `oracle_search` | hybrid / vector / text recall |
| `oracle_add` | store a durable fact (embedded + indexed) |
| `oracle_update` | replace a fact by `memory_id` |
| `oracle_delete` | delete by `memory_id` |
| `oracle_chunk` | split inline text or a disk file in-database |
| `oracle_sync_skills` | scan, parse, and index skill libraries into vector memory |
| `oracle_load_skill` | load or list skills from the shared Oracle skill corpus at runtime |

## Skills

- **`oracle-skill-corpus`** — discover and materialize shared-corpus skills dynamically from Oracle ADB at runtime without local disk installation.
- **`oracle-import`** — bulk-ingest conversation archives (ShareGPT / Claude / ChatGPT / JSON) and large documents into ADB without LLM rate limits or context inflation.

## Shared Multi-Server Skill Corpus

The shared Oracle skill corpus turns Oracle Autonomous AI Database 26ai into a central, multi-server skill source of truth accessible across all Hermes instances. Any server profile can discover and reconstruct skills at runtime without requiring local disk presence.

### Storage Layout

Per ingested skill `S`:

| `memory_id` | Role | Description |
|---|---|---|
| `skill:{host}:{S}:c0` | `skill` | Meta row: `[skill_ingest] [skill:S] S — {description}\nWhen to use: {triggers}\nTags: {tags}` |
| `skill:{host}:{S}:c1 ... cN-1` | `skill` | Body chunks: `[skill_ingest] [skill:S] S (chunk i/N):\n{text}` |
| `skill:{host}:{S}:cN ...` | `skill` | Reference file chunks: `(reference: rel/path) (chunk i/M)` |

- **Metadata** (JSON): `category, chunk_index, hash (sha256 of source file), kind=skill_ingest, mtime, path, skill_name, total_chunks, version, host, target=memory`
- **Reconstruction fidelity**: Chunks are 80 whole words joined by single spaces with 10-word overlap. Reassembled text is word-accurate and lossless at word level (line/markdown formatting flattened).
- **Backward compatibility**: Legacy single-row skills (pre-c0 era, no `:cN` suffix) are treated as meta rows and return with status `"meta_only"`.

### Using `oracle_load_skill`

#### Listing / Discovering Skills (`action="list"`)

Search the catalog by substring or paginate:

```json
{
  "action": "list",
  "filter": "address",
  "offset": 0
}
```

Returns `{count_in_page, offset, more, skills: [{name, category, description}]}`.

#### Materializing a Skill (`action="fetch"`)

Fetch and reconstruct the full skill document:

```json
{
  "action": "fetch",
  "skill": "usps-address-validator"
}
```

Returns `{status: "ok", skill, category, source_host_path, total_chunks, chunk_count, word_count, markdown, references: {rel_path: text}, content_hash, mtime}`.
If cross-host variants exist with different paths, returns `status: "multiple_variants"` with a variants list to disambiguate.

### Zombie-Safe Host-Scoped Pruning

In a shared multi-server table, deletions must be host-scoped:
- **Foreign-host rows are never touched** by another server.
- **Locally deleted skills** are pruned from the shared database using:
  ```bash
  python3 scripts/sync_skills.py --prune-host /path/to/local/skills/ --prune-limit 500
  ```
- **Dry-run verification**:
  ```bash
  python3 scripts/sync_skills.py --dry-run
  ```

### Live Lifecycle Syncing

The `on_skill_lifecycle` hook synchronizes changes in real-time as skills are managed by the agent:
- Actions `created`, `edited`, `patched`, `installed`, `restored` immediately format and upsert skill chunks in the background. Idempotency pre-checks content hashes before writing.
- Actions `archived` safely delete corpus rows only if the `SKILL.md` file has been removed from the local disk.

### Legacy Host-Tag Migration

To migrate older untagged rows (`skill:{name}:cN`) to host-tagged identifiers (`skill:{host}:{name}:cN`), run the one-shot migration utility:

```bash
python3 scripts/sync_skills.py --migrate-host-tags
```

## Vector Index Maintenance & ORA-51815

Under heavy write or bulk ingest workloads, queries against in-memory HNSW vector indexes may encounter:
```text
ORA-51815: INMEMORY NEIGHBOR GRAPH HNSW vector index snapshot is too old.
```
- **Transient & Self-Healing**: `ORA-51815` is a transient concurrency condition while the in-memory neighbor graph refreshes its snapshot. It needs **waiting or retrying**, NOT rebuilding.
- **Automatic Read Retry & Degradation**: Hybrid search and vector search automatically retry queries encountering `ORA-51815` with exponential backoff (3 attempts, 0.5s backoff). On exhausted retries, hybrid search gracefully degrades to Oracle Text search rather than failing.
- **Index Rebuild Helper (`scripts/rebuild_index.py`)**: Rebuilding a vector index is a last resort. Use the canonical helper:
  ```bash
  python3 scripts/rebuild_index.py --table HERMES_AGENT_MEMORY
  ```
  This drops and recreates the index using the canonical `VECTOR_INDEX_DDL` constant:
  ```sql
  CREATE VECTOR INDEX {index_name} ON {table_name} (embedding) ORGANIZATION INMEMORY NEIGHBOR GRAPH
  ```
  and automatically refreshes optimizer statistics via `DBMS_STATS.GATHER_TABLE_STATS`.

## What's new in v1.1.1

Sterile distribution release (full details in `RELEASE_NOTES_1.1.1.md`) — no provider behavior change:

- **Identifier scrub**: no fleet hostnames, operator/developer usernames, absolute home/build paths, or production OCI region + service descriptors anywhere in the shipped tree — the plugin installs cleanly on any host without leaking its origin.
- **Clean install scan**: the tree now clears the Hermes install-time security scanner with a `safe` verdict (0 critical, 0 high — previously a `caution` verdict that required a confirmation prompt on every install/update).

## What's new in v1.1.0.1

Maintenance release — fleet-sync hardening (full details in `CHANGELOG.md`):

- **Sync interpreter guard**: `scripts/sync_skills.py` re-executes into a venv that can import `oracledb` (import probe, loop-guarded) instead of dying with `No module named 'oracledb'` when launched from a bare interpreter.
- **Canonical skill meta-row ID**: meta sync rows now use `skill:{host}:{name}:c0` (matching the full-mode chunk-0 row), eliminating `ORA-00001` duplicates and restoring second-run idempotency.
- **Write-time budget**: pooled connection `call_timeout` defaults to `max(search, ingest)`; bulk ONNX inserts no longer die at the 13 s search default (`DPY-4024`). Read paths keep the tight 13 s window.
- **Transient-embedding fallback**: server-side transient embedding errors (`ORA-01013`/`ORA-04031`/…) now degrade to NULL-embedding insert + background backfill instead of a 60 s cooldown + lost row — across all three write ladders (`_insert`, `_insert_many`, `oracle_update`).
- **Pool fingerprint** now includes the timeout budget, so timeout-config changes rebuild the pool in long-lived processes.
- **Self-deadlock fix in live sync**: the sync's deletes are committed before the provider's `_insert_many` re-inserts the same rows on a second pool connection — the old order parked the run forever on its own uncommitted row locks.

## What's new in v1.1.0

- **Shared Cross-Server Skill Corpus (`oracle_load_skill`)**:
  - Full skill discovery and reconstruction path (`action="fetch"` / `action="list"`) allowing any Hermes host to discover and load any skill across the 2,300+ fleet-wide library dynamically at runtime, without local disk installation.
  - Word-accurate chunk reassembly reproducing source documents from 80-word, 10-word overlap chunks including bundled reference files.
  - Multi-variant resolution with explicit path and variant disambiguation.
- **Live Lifecycle Ingestion (`on_skill_lifecycle`)**:
  - In-process hook integration syncing skills into Oracle 26ai ADB immediately upon `created`, `edited`, `patched`, `installed`, or `restored` events.
  - SHA256 hash idempotency preventing redundant DB round-trips when skill contents are unchanged.
  - Safe retirement on `archived` events when local files are removed.
- **Zombie-Safe Host-Scoped Pruning (`scripts/sync_skills.py`)**:
  - Host-scoped pruning keyed on `(skill_name, metadata.path LIKE host_root)` pairs—foreign-host skills are strictly preserved, while locally deleted skills are cleaned up.
  - Host-tagged `memory_id` and `conv_ref` convention (`skill:{host}:{name}:cN`) preventing accidental sibling host overwrites.
  - Dead SQL leg 3 removed; one-shot migration utility (`--migrate-host-tags`).
- **Resilient & Idempotent Initialization**:
  - Idempotent `initialize()` preventing duplicate connection pools and eliminating `pool not initialized` warnings during multi-context instantiation.
  - Stale connection detection and automatic re-acquisition for ADB restart recovery.
  - Provider health diagnostic reporting (`health()`).
- **Separated Ingest Timeout**:
  - Autonomous ingestion timeout (`HERMES_ORACLE_INGEST_TIMEOUT`, default 600s) decoupled from query search timeouts (`HERMES_ORACLE_SEARCH_TIMEOUT_MS`, default 1300ms) to avoid batch timeouts during vector materialization.

## Contributing

Pull requests welcome. First-time contributors sign the [CLA](CLA.md) by commenting on the PR:

```text
I have read the CLA Document and I hereby sign the CLA
```

## License

[Business Source License 1.1](LICENSE) — free for personal, research, and organizations under $1M annual revenue; converts to Apache-2.0 on 2030-10-11.
