# Hermes Agent Oracle 26ai Memory Provider — Installation Guide

![Version](https://img.shields.io/badge/version-1.0.9.4.2-blue.svg)
![License](https://img.shields.io/badge/license-BSL--1.1-orange.svg)

> **CRITICALITY NOTICE**: v1.0.9.4.2 updates are CRITICAL to functionality. Skill-path remediation (single-source telemetry SQL filter unblocking skill_ingest recall, isolated dataclass module loader for in-process oracle_sync_skills), generic multi-profile synchronization, and zero drift across repository, installed plugin, and all Hermes Agent profile environments.

This guide provides complete instructions for installing, configuring, and verifying the `hermes-oracle-memory` plugin for [Hermes Agent](https://github.com/nousresearch/hermes-agent), leveraging Oracle Autonomous AI Database (ADB) 26ai.

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
  - Native 768-dimensional `VECTOR(768, FLOAT32)` storage, in-database chunking via `DBMS_VECTOR.UTL_TO_CHUNKS`, and IVF / HNSW In-Memory Neighbor Graph indexing.
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
   - Database user account (e.g., `ADMIN`) with privileges to create tables, indexes, and mining models.
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
OCI_DB_USER=ADMIN
OCI_DB_PASSWORD="YourDatabasePasswordHere"
OCI_DB_DSN="(DESCRIPTION=(RETRY_COUNT=20)(RETRY_DELAY=3)(ADDRESS=(PROTOCOL=tcps)(PORT=1521)(HOST=adb.us-ashburn-1.oraclecloud.com))(CONNECT_DATA=(SERVICE_NAME=xxxx_low.adb.oraclecloud.com))(SECURITY=(SSL_SERVER_DN_MATCH=yes)))"

# =============================================================================
# mTLS Wallet Configuration (Required for mTLS connections)
# =============================================================================
OCI_WALLET_LOCATION=~/.oci/wallet
OCI_WALLET_PASSWORD="YourWalletPassword"

# =============================================================================
# Primary In-Database ONNX Model
# =============================================================================
HERMES_ORACLE_ONNX_MODEL=BGE_BASE_EN_V1_5
```

---

## Step 5: Verification & Testing

Verify that the database connection, table initialization, and embedding pipelines operate properly.

### 1. Run Provider Verification Script

```bash
python3 scripts/verify_provider.py
```

The script verifies:
- Oracle ADB 26ai database connection and thin/thick mode initialization.
- Table schema setup (`HERMES_AGENT_MEMORY`) with `VECTOR(768, FLOAT32)` and Oracle Text search indexes.
- In-database ONNX inference (`VECTOR_EMBEDDING`) and fallback routing.
- Storage and hybrid recall of test memories.

### 2. Run Test Suite

```bash
pytest tests/
```

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

When set, the provider skips runtime DDL attempts (`_ensure_schema()`) and verifies that required tables and indexes exist (`_verify_schema()`).

### Prompt Injection Hardening

Prefetched memories injected into prompt context are isolated inside `<retrieved_memory id="..." target="..." score="...">` XML boundary tags with metadata attribute escaping (`html.escape`) and ASCII control-character stripping (`[\x00-\x08\x0b-\x1f\x7f]`).

### Credential Protection

Database credentials (such as `OCI_DB_PASSWORD`) are automatically redacted from error outputs and initialization exception logs.

