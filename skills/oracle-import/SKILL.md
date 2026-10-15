---
name: oracle-import
description: Import conversation archives and documents into Oracle Autonomous AI Database 26ai memory without hitting LLM rate limits.
version: 1.0.7
author: Hermes Community
metadata:
  hermes:
    tags: [database, memory, oracle, import, sharegpt]
---

# Oracle 26ai Conversation & Document Import

Import conversation history archives (ShareGPT, ChatGPT, Claude exports) and large documents into **Oracle Autonomous AI Database (ADB) 26ai** vector memory without exhausting LLM context windows or triggering upstream API rate limits (such as NVIDIA NIM HTTP 429).

## When to Use

- Importing historical conversations (`conversations.json`, `oracle_import.json`, ShareGPT format).
- Ingesting large text archives, documentation, or log files into Oracle 26ai memory.
- Bypassing LLM context limits and rate limits during bulk data migration.
- Troubleshooting Oracle ADB mTLS wallet connections and driver configurations during import.

## Prerequisites

Ensure ADB credentials and wallet location are configured in `~/.hermes/.env` or active profile environment:

- `OCI_DB_USER` — ADB username (e.g. `ADMIN`)
- `OCI_DB_PASSWORD` — ADB password
- `OCI_DB_DSN` — TNS alias or connect descriptor string (e.g. `dbname_high`)
- `OCI_WALLET_LOCATION` (optional) — Path to the wallet directory containing `cwallet.sso` and `tnsnames.ora`. Auto-detected at `~/.oci/wallet` and `~/.hermes/oracle_wallet`.
- `OCI_WALLET_PASSWORD` (optional) — Wallet password if encrypted.
- `python-oracledb` >= 4.0.2 installed in the environment.

## How to Run

### Option 1: Ingest via `oracle_chunk` with `file_path` (Hermes Tool)

Hermes agents can invoke `oracle_chunk` with a local `file_path` directly. Instead of streaming multi-megabyte payloads through the LLM prompt, the tool reads the file locally, parses conversation structures if present, and executes in-database chunking with `DBMS_VECTOR.UTL_TO_CHUNKS`:

```json
{
  "file_path": "/home/rocky/sharedconv/oracle_import.json",
  "max_words": 1000,
  "overlap": 10
}
```

### Option 2: Standalone Bulk Ingestion Script

For large archives (e.g. hundreds of conversations or >10 MB), run the standalone migration script included with this skill. It connects directly to Oracle 26ai ADB and inserts memory turns in configurable batches with zero LLM API consumption:

```bash
# Ingest ShareGPT, Claude, or summary archive
python3 skills/oracle-import/scripts/import_conversations.py /home/rocky/sharedconv/oracle_import.json

# Specify batch size and session ID
python3 skills/oracle-import/scripts/import_conversations.py /home/rocky/sharedconv/conversations.json --batch-size 50 --session-id "archive-2026-06"
```

## Quick Reference

| Format | Structure | Parsing Behavior |
|---|---|---|
| **ShareGPT** | `{"conversations": [{"from": "...", "value": "..."}]}` | Turns extracted and prefixed with `[ROLE]` |
| **Claude Export** | `[{"name": "...", "summary": "...", "chat_messages": [...]}]` | Extracts titles, summaries, and user/assistant messages |
| **Summary Export** | `{"conversations": [{"from": "system", "value": "..."}]}` | Extracted as system/session memory turns |
| **Plain Text** | `.txt`, `.md`, logs | Split recursively via `UTL_TO_CHUNKS` |
| **Truncated JSON** | Abrupt EOF / partial stream | Incremental recovery of complete leading JSON objects |

## Procedure

1. **Verify Database Connectivity & Wallet**:
   Ensure wallet files exist in `~/.oci/wallet/` or `~/.hermes/oracle_wallet/` (`cwallet.sso`, `tnsnames.ora`).
2. **Choose Ingestion Method**:
   - For single documents or prompt-driven chunking: call `oracle_chunk(file_path=...)`.
   - For bulk multi-conversation imports (>1MB): run `python3 skills/oracle-import/scripts/import_conversations.py <path>`.
3. **Verify Recall**:
   Query Oracle memory using `oracle_search(query="...", top_k=5)` to verify turns and chunks are discoverable.

## Pitfalls

- **NIM HTTP 429 Rate Limits**: Avoid passing file contents directly into chat messages or `oracle_chunk(content=...)`. Always pass `file_path`.
- **`DPY-6005: Listener refused connection`**: Wallet files missing, unreadable, or TLS certificate mismatch. Check permissions (`chmod 600`) and wallet files.
- **`DPY-6000: Connection timeout`**: Verify Autonomous Database is in **Available** state in OCI Console and egress on port 1522 is open.
- **`ORA-01460: unimplemented or unreasonable conversion requested`**: Occurs if large strings (>32KB) are bound to CLOB parameters without `cur.setinputsizes(oracledb.DB_TYPE_CLOB)`. The provider automatically sets CLOB input sizes.

## Verification

Confirm imported items are indexed in Oracle ADB:
```python
provider = OracleMemoryProvider()
provider.initialize(session_id="test")
results = provider._search(query="ITAHM", top_k=3)
assert len(results) > 0
```
