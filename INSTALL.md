# Hermes Agent Oracle 26ai Memory Provider — Installation Guide

![Version](https://img.shields.io/badge/version-1.1.1.5-blue.svg)
![License](https://img.shields.io/badge/license-BSL--1.1-orange.svg)

> **CRITICALITY NOTICE**: Shared-corpus read path (oracle_load_skill), live sync on skill create/update (on_skill_lifecycle ingest), zombie-safe host-scoped pruning, idempotent init, separated ingest timeout.

This guide provides complete instructions for installing, configuring, and verifying the `hermes-oracle-memory` plugin (v1.1.1.5) for [Hermes Agent](https://github.com/nousresearch/hermes-agent), leveraging Oracle Autonomous AI Database (ADB) 26ai.

---

## Architectural Highlights

### Token & API Cost Reduction

`hermes-oracle-memory` is engineered to minimize external API dependencies and drastically curtail LLM token expenditures:

- **Zero External Embedding Costs via In-Database ONNX Inference**:
  - Uses `VECTOR_EMBEDDING` natively inside Oracle Database 26ai with `BGE_BASE_EN_V1_5` (`BAAI/bge-base-en-v1.5`).
  - Embedding computation is executed entirely within the database engine kernel via ONNX runtime, eliminating recurring token costs and external latency from paid third-party embedding APIs.
- **In-Memory Embedding Deduplication**:
  - The `_embed_many` batch ingestion pipeline detects and coalesces duplicate texts prior to embedding calls.
  - Identical turns, prompt fragments, and redundant context entries are embedded exactly once, preventing wasted API token spend.
- **Accurate Hybrid Retrieval (Oracle Text + Vector)**:
  - Fuses dense vector similarity (`VECTOR_DISTANCE`) with exact lexical full-text scoring (`CONTAINS` / `SCORE`) in a single round-trip.
  - Filters and ranks only top-relevance facts, preventing prompt window bloat and eliminating the token cost of dumping noisy, oversized context blocks into the LLM.

### Native Oracle Cloud (OCI) Architecture Integration

The provider delivers enterprise-grade integration across Oracle Cloud Infrastructure:

- **Direct OCI Autonomous AI Database 26ai Vector Capabilities**:
  - Native 768-dimensional `VECTOR(768, FLOAT32)` storage, in-database chunking via `DBMS_VECTOR.UTL_TO_CHUNKS`, and an in-memory HNSW vector index (`ORGANIZATION INMEMORY NEIGHBOR GRAPH`).
- **Native OCI GenAI Service Fallback**:
  - Automatic fallback to OCI Generative AI inference (`cohere.embed-v4.0`) via native OCI Instance Principals and API signing when running on OCI Compute or container instances—no static API keys required.
- **Connection and Signer Caching Across Agent Sessions**:
  - The OCI GenAI client and `InstancePrincipalsSecurityTokenSigner` security signer are cached and reused across agent sessions, removing client initialization latency and cryptographic token regeneration overhead.

---

## Prerequisites

Before starting, ensure you have:

1. **Python**: Version 3.10 or higher.
2. **Oracle Autonomous AI Database 26ai**:
   - Provisioned ADB-S or ADB-D instance on Oracle Cloud Infrastructure (OCI).
   - Database user account with privileges to create tables, indexes, and mining models (ADB's default is `ADMIN`).
3. **Database Credentials & Connectivity**:
   - Database connection string (DSN / TNS descriptor).
   - If using mTLS: Downloaded client credentials wallet (`Wallet_*.zip`) extracted to a local directory (e.g. `~/.oci/wallet`).
4. **Hermes Agent**: Installed and configured (`hermes` CLI available in your path).

---

## Step 1: Install the Provider Plugin

You can install `hermes-oracle-memory` either as a plugin directly inside your Hermes home or as an editable Python package.

### Option A: Install into Hermes Plugins Directory (Recommended)

Clone or copy the repository directly into your Hermes plugins directory:

```bash
# Target directory
mkdir -p ~/.hermes/plugins
git clone https://github.com/xl3v3r/hermes-oracle-memory.git ~/.hermes/plugins/oracle
```

Install required Python dependencies:

```bash
pip install "oracledb>=4.0.2" oci
```

### Option B: Editable Python Package Install

```bash
git clone https://github.com/xl3v3r/hermes-oracle-memory.git
cd hermes-oracle-memory
pip install -e .
```

---

## Step 2: Configure Hermes Agent

Enable the `oracle` memory plugin in Hermes Agent configuration.

### Edit `~/.hermes/config.yaml`

Ensure `oracle` is listed under `plugins.enabled`:

```yaml
plugins:
  enabled:
    - oracle
```

Alternatively, configure via Hermes CLI:

```bash
hermes config set memory.provider oracle
```

---

## Step 3: Setup In-Database ONNX Model (BGE_BASE_EN_V1_5)

To achieve **zero external embedding API costs**, configure in-database ONNX inference directly on Oracle ADB 26ai:

1. Export and load `BAAI/bge-base-en-v1.5` into your database:
   ```bash
   python3 scripts/setup_onnx_model.py
   ```
   The helper scripts read `OCI_DB_*` / `OCI_WALLET_*` from the environment. To use the settings from your Hermes env file (Step 4), pass it explicitly: `python3 scripts/setup_onnx_model.py --env-file <path to the env file>`.

   This exports the model with dynamic token slicing (bounded to 512 tokens to avoid `ORA-54401`), stages it via `DATA_PUMP_DIR`, and registers it with `DBMS_VECTOR.LOAD_ONNX_MODEL`.

2. Specify the ONNX model name in your environment:
   ```bash
   echo 'HERMES_ORACLE_ONNX_MODEL=BGE_BASE_EN_V1_5' >> ~/.hermes/.env
   ```

---

## Step 4: Configure Environment & Credentials (`~/.hermes/.env`)

Configure your database connection, wallet path, and fallback embedding providers in `~/.hermes/.env`:

```bash
# =============================================================================
# Oracle ADB 26ai Database Connection (Required)
# =============================================================================
OCI_DB_USER="<your-db-user>"
OCI_DB_PASSWORD="<your-db-password>"
OCI_DB_DSN="(DESCRIPTION=(RETRY_COUNT=20)(RETRY_DELAY=3)(ADDRESS=(PROTOCOL=tcps)(PORT=1522)(HOST=adb.<your-region>.oraclecloud.com))(CONNECT_DATA=(SERVICE_NAME=<service-name>))(SECURITY=(SSL_SERVER_DN_MATCH=yes)))"

# =============================================================================
# mTLS Wallet Configuration (Required for mTLS connections)
# =============================================================================
OCI_WALLET_LOCATION=~/.oci/wallet
OCI_WALLET_PASSWORD="YourWalletPassword"

# =============================================================================
# Primary In-Database ONNX Model
# =============================================================================
HERMES_ORACLE_ONNX_MODEL=BGE_BASE_EN_V1_5

# =============================================================================
# Timeouts (optional — defaults shown)
# =============================================================================
# HERMES_ORACLE_SEARCH_TIMEOUT_MS=30000      # recall query clamp (ms)
# HERMES_ORACLE_POOL_WAIT_TIMEOUT_MS=20000   # max wait for a pooled connection (ms)
# HERMES_ORACLE_POOL_IDLE_TIMEOUT=300        # idle session lifetime (s)
# HERMES_ORACLE_PREFETCH_WAIT=3              # per-turn recall wait (s, 0.1-7)
# HERMES_ORACLE_INGEST_TIMEOUT=600           # bulk ingest / write budget (s)
```

> Upgrading from 1.1.1.2 or older: if your `.env` pins `HERMES_ORACLE_SEARCH_TIMEOUT_MS=13000`, that value now actually takes effect. Remove it (or set `30000`) to use the new default.

---

## Step 5: Verification & Testing

Verify that the database connection, table initialization, and embedding pipelines operate properly.

### 1. Run Provider Verification Script

```bash
export OCI_DB_USER=... OCI_DB_PASSWORD=... OCI_DB_DSN=...
python3 tests/verify_provider.py
```

The script reads the `OCI_*` settings from the environment. To take them from a file instead, add `--env-file <path>` (for example `--env-file "$HERMES_HOME/.env"`); it reads no file on its own.

It starts the provider and checks that:
- the provider connects and the memory table is usable (an empty schema is bootstrapped first);
- the in-database ONNX model returns a 768-dimension vector;
- a memory can be stored, read back, found by vector, hybrid and text search, updated and deleted.

Every row the run writes carries a session id unique to that run (`verify-<random>`), and the run deletes them all before it exits, also when a check fails. It asserts nothing about rows that were already in the table, so it gives the same result on an empty database and on one in use.

| Option | Effect |
|---|---|
| `--env-file <path>` | Read `KEY=VALUE` settings from a file. Values already in the environment win. |
| `--table <name>` | Use another table for this run, for example a scratch table. |
| `--text-wait <seconds>` | How long to wait for a new row to appear in text search (default 30). |
| `--strict` | Fail if the row is not in text search within that time (otherwise a warning). |

Exit status: `0` every check passed, `1` a check failed, `2` the script could not run (settings missing).

> **Rows left by the script of 1.1.1.5 and older.** That version stored three rows per run under the session id `test-verification-session` and never removed them. They are ordinary facts, so they can be recalled. To remove them, call `oracle_delete(session_id="test-verification-session")` from a Hermes session.

The same check runs under pytest when you opt in. Without `HERMES_ORACLE_LIVE_TESTS=1` it is skipped, whatever credentials are in the environment:

```bash
HERMES_ORACLE_LIVE_TESTS=1 python -m pytest -q tests/test_provider_live.py
```

#### Against a local Oracle AI Database Free container

`tests/smoke_db/` holds what the CI job uses to run the script against an empty database: a tiny embedding model (about 11 KB, 768 dimensions, for testing only) and a script that creates an empty schema user and loads the model.

```bash
python -m pip install oracledb onnx
docker run -d --name oracle-free -p 127.0.0.1:1521:1521 -e ORACLE_PWD=<admin password> \
  container-registry.oracle.com/database/free:23.26.4.0-lite

export OCI_DB_DSN=127.0.0.1:1521/FREEPDB1 OCI_DB_USER=HERMES_SMOKE_USER OCI_DB_PASSWORD=<user password>
export HERMES_ORACLE_ONNX_MODEL=HERMES_SMOKE_EMBED
SMOKE_DB_ADMIN_PASSWORD=<admin password> python tests/smoke_db/prepare_database.py
python tests/verify_provider.py
```

`prepare_database.py` refuses a database that is not on the local machine and never drops a user or a table. The schema user it creates has `CREATE SESSION`, `CREATE TABLE`, `CREATE MINING MODEL` (to load the model) and a quota on its tablespace. The smoke test passes with these privileges alone.

### 2. Run Test Suite

The unit tests need no database. Run them with `HOME` pointed at a scratch directory and `HERMES_HOME` unset, so nothing can reach your real `~/.hermes`:

```bash
env -u HERMES_HOME HOME="$(mktemp -d)" python -m pytest -q
```

The suite also drops an inherited `HERMES_HOME` when it starts (the Hermes Desktop shell exports one), so a run without `env -u HERMES_HOME` no longer fails.

> **Warning:** do not run the test suite of v1.1.1.2 or older on a host with an installed plugin. Its drift test copied the checkout over `~/.hermes/plugins/oracle` and every profile copy. From v1.1.1.3 the drift check is read-only and opt-in: `HERMES_ORACLE_DRIFT_DIR=~/.hermes/plugins/oracle python -m pytest -q -k zero_drift`.

### 3. Check Hermes Agent Status

Check Hermes gateway and daemon services:

```bash
hermes gateway status
hermes dashboard --status
```

Test a session interactively:

```bash
hermes chat -q "Remember that my preferred deployment target is Oracle Cloud OCI."
hermes chat -q "Where is my preferred deployment target?"
```

---

## Step 6: Upgrades & Legacy Data Backfill

If upgrading from an earlier version (e.g. 1536-dimensional vectors or null embeddings), run the parallel backfill utility:

```bash
python3 scripts/backfill_embeddings.py --workers 4 --batch-size 32
```

(Export the `OCI_*` settings first, or add `--env-file <path to the env file>`; no script reads the Hermes env file on its own.)

The backfill utility:
- Multithreads batch processing across database sessions.
- Automatically tracks checkpoints in `backfills/backfill_progress_768.json`.
- Refreshes vector indexes and table optimizer statistics upon completion (`DBMS_STATS.GATHER_TABLE_STATS`).

---

## Step 7: Security & Least-Privilege Operation

### Restricted DB Environments (`REQUIRE_EXISTING` Schema Policy)

In enterprise environments where the database schema is managed via CI/CD migrations and application accounts do not possess `CREATE TABLE` or `CREATE INDEX` privileges, configure:

```bash
HERMES_ORACLE_SCHEMA_POLICY=REQUIRE_EXISTING
```

When set, the provider runs no DDL at start-up. It checks that the table can be queried and looks up its indexes (`_verify_schema()`):

- A text index in the `FAILED` state stops the provider with an error, because such an index rejects every write (`ORA-29861`).
- A missing text or vector index is logged as a warning and listed under `schema_warnings` in `health()`. Search keeps working within the limits the warning names.

Create the schema with the statements the bootstrap runs. Replace `HERMES_AGENT_MEMORY` if `HERMES_ORACLE_TABLE` names another table:

```sql
CREATE TABLE HERMES_AGENT_MEMORY (
    memory_id VARCHAR2(128) PRIMARY KEY,
    session_id VARCHAR2(128),
    agent_id VARCHAR2(64),
    role VARCHAR2(64),
    content CLOB,
    embedding VECTOR(768, FLOAT32),
    created_at TIMESTAMP DEFAULT SYSTIMESTAMP,
    target VARCHAR2(32) DEFAULT 'memory',
    metadata JSON,
    content_txt VARCHAR2(4000),
    embedding_model VARCHAR2(64),
    embedding_updated_at TIMESTAMP(6),
    turn_index NUMBER,
    chunk_index NUMBER,
    turn_id VARCHAR2(64),
    conv_ref VARCHAR2(512)
);

CREATE SEARCH INDEX HERMES_AGENT_MEMORY_TXT_IDX ON HERMES_AGENT_MEMORY (content);

CREATE VECTOR INDEX HERMES_AGENT_MEMORY_VEC_IDX ON HERMES_AGENT_MEMORY (embedding) ORGANIZATION INMEMORY NEIGHBOR GRAPH;
```

- **Text index.** A plain Oracle Text search index on `content`. The database maintains it in the background, so a new row appears in text search a few seconds after it is committed. Vector search and `oracle_get` see the row at once.
- **Vector index.** An in-memory HNSW index. It needs the vector pool (`vector_memory_size`); without one the statement fails with `ORA-51962` and vector search compares every row, which is acceptable for a small table. An IVF index (`ORGANIZATION NEIGHBOR PARTITIONS`) needs no vector pool and works with the plugin as well.
- **JSON search index.** Not required. The bootstrap does not create one on `metadata`.

#### Text index left `FAILED` by 1.1.1 to 1.1.1.5

Those releases created the text index with `FOR JSON` on the `content` CLOB. On an empty schema that statement fails and leaves the index in the `FAILED` state, and every write is then rejected with `ORA-29861`. From 1.1.2 the bootstrap drops that index and creates it again. Under `REQUIRE_EXISTING` it runs no DDL, so the provider stops with an error that names the two statements to run:

```sql
DROP INDEX HERMES_AGENT_MEMORY_TXT_IDX FORCE;
CREATE SEARCH INDEX HERMES_AGENT_MEMORY_TXT_IDX ON HERMES_AGENT_MEMORY (content);
```

### Prompt Injection Hardening

Prefetched memories injected into prompt context are isolated inside `<retrieved_memory id="..." target="..." score="...">` XML boundary tags with metadata attribute escaping (`html.escape`) and ASCII control-character stripping (`[\x00-\x08\x0b-\x1f\x7f]`).

### Credential Protection

Database credentials (such as `OCI_DB_PASSWORD`) are automatically redacted from error outputs and initialization exception logs.

