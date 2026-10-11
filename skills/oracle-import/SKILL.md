---
name: oracle-import
version: 1.0.4
description: Import conversation archives (ShareGPT, ChatGPT, Claude) and large documents into Oracle Autonomous AI Database 26ai memory without hitting LLM rate limits.
trigger: oracle import, import conversations, import to oracle, oracle adb import, oracle memory import, oracle chunk file
tags:
  - database
  - memory
  - oracle
  - import
  - sharegpt
---

# Oracle 26ai Conversation & Document Import

This skill provides workflows, tools, and scripts to import conversation history archives (such as ShareGPT, ChatGPT, Claude exports) and large documents into **Oracle Autonomous AI Database (ADB) 26ai** vector memory without exhausting LLM context windows or triggering upstream API rate limits (e.g., NVIDIA NIM, OpenRouter, Anthropic HTTP 429).

---

## 1. When to Use

- Importing historical conversations (`conversations.json`, `oracle_import.json`, ShareGPT format).
- Ingesting large text archives, documentation, or log files into Oracle 26ai memory.
- Bypassing LLM context limits and rate limits during bulk data migration.
- Troubleshooting Oracle ADB mTLS wallet connections and driver configurations during import.

---

## 2. Workflows

### Method A: Ingest via `oracle_chunk` with `file_path` (Hermes Tool)

Hermes agents can invoke `oracle_chunk` with a local `file_path` directly. Instead of loading multi-megabyte payloads into the LLM context prompt, the tool reads the file locally, parses conversation structures if present, and executes in-database chunking with `DBMS_VECTOR.UTL_TO_CHUNKS`:

```json
{
  "file_path": "/home/rocky/sharedconv/oracle_import.json",
  "max_words": 1000,
  "overlap": 10
}
```

**Supported file formats:**
- **ShareGPT JSON**: `{"conversations": [{"from": "human", "value": "..."}]}` or multi-session lists.
- **Claude / ChatGPT JSON**: Arrays with `chat_messages` or session messages.
- **Plain Text / Markdown**: Text and log files chunked recursively.

### Method B: Standalone Bulk Ingestion Script

For large archives (e.g. hundreds of conversations or >10 MB), use the standalone import script included with this skill. It connects directly to Oracle 26ai ADB and inserts memory turns in configurable batches with zero LLM API consumption:

```bash
# Basic usage
python3 skills/oracle-import/scripts/import_conversations.py /home/rocky/sharedconv/oracle_import.json

# Specify batch size and session ID
python3 skills/oracle-import/scripts/import_conversations.py /path/to/archive.json --batch-size 50 --session-id "archive-import-001"
```

---

## 3. Oracle ADB Connection & Wallet Troubleshooting

### Environment Variables
Ensure the following credentials are set in `~/.hermes/.env` or the active profile:
- `OCI_DB_USER` — ADB username (e.g., `ADMIN`)
- `OCI_DB_PASSWORD` — ADB password
- `OCI_DB_DSN` — TNS alias or connect descriptor string (e.g. `dbname_high`)
- `OCI_WALLET_LOCATION` (optional) — Path to the wallet directory containing `cwallet.sso` and `tnsnames.ora` (auto-detected in `~/.oci/wallet` and `~/.hermes/oracle_wallet`).
- `OCI_WALLET_PASSWORD` (optional) — Wallet password if encrypted.

### Resolving Driver & Wallet Errors
- **`DPY-6005: Listener refused connection`**:
  - Wallet files are missing, unreadable, or TLS certificate expired.
  - Verify wallet directory contains `cwallet.sso`, `ewallet.p12`, `tnsnames.ora`, and `sqlnet.ora`.
  - Permissions should be `chmod 600` on wallet files.
- **`DPY-6000: Connection timeout`**:
  - OCI Console: Ensure Autonomous Database is in **Available** state (not paused/stopped).
  - Verify TCP/1522 egress is allowed through network security lists / firewalls.
- **`AUTH_TYPE_OCI` / Thin Mode Incompatibilities**:
  - Use `oracledb` 4.0.2+ which supports mTLS wallets in thin mode with `wallet_location` and `config_dir`.
  - If thick mode is required, call `oracledb.init_oracle_client(lib_dir="...")` with Oracle Instant Client libraries.

---

## 4. Performance & Batching Guidelines

- **Vector Backfill**: Embeddings are generated in background batches of 32 (`_EMBED_BATCH`) to prevent network bottlenecks.
- **Text Search Immediate**: Full-text search via Oracle Text `CONTAINS` is active immediately upon commit, even while vector embeddings are backfilling.
- **Chunk Size**: For general conversation recall, `max_words=80` with `overlap=10` is optimal. For long session summaries, `max_words=500` to `1000` is recommended.
