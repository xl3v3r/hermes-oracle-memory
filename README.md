# Oracle 26ai Memory Provider

![Version](https://img.shields.io/badge/version-1.0.4-blue.svg)

## About

`hermes-oracle-memory` is a high-performance external memory provider plugin for [Hermes Agent](https://github.com/nousresearch/hermes-agent), leveraging Oracle Autonomous AI Database (ADB) 26ai. It provides hybrid vector and full-text memory retrieval across conversations, enabling low-latency, scalable, and durable long-term memory for AI agents without relying on local SQLite or legacy memory systems like Mnemosyne.

## What's New in v1.0.4

- **`oracle-import` Hermes Skill**: Import conversation history archives (ShareGPT, Claude, ChatGPT, and overview JSON files) and large documents directly into Oracle 26ai ADB memory without exhausting LLM context windows or triggering API rate limits (e.g. HTTP 429).
- **Direct File Chunking via `oracle_chunk` (`file_path`)**: The `oracle_chunk` tool now supports reading files directly from disk via `file_path`, running in-database chunking with `DBMS_VECTOR.UTL_TO_CHUNKS` and avoiding multi-megabyte prompt token inflation.
- **Multi-Format & Truncated JSON Extraction**: Native turn extraction across ShareGPT, Claude, and overview structures with automatic recovery of complete leading JSON objects in truncated files.
- **ADB Wallet Auto-Detection**: Automatic discovery of Oracle ADB mTLS wallet credentials in `~/.oci/wallet` and `~/.hermes/oracle_wallet` across thin and thick modes.

## Requirements

- `oracledb` (thin mode or thick mode with wallet)
- Env in `$HERMES_HOME/.env`: `OCI_DB_USER`, `OCI_DB_PASSWORD`, `OCI_DB_DSN`
- Optional wallet path: `OCI_WALLET_LOCATION` or `TNS_ADMIN` (auto-detected in `~/.oci/wallet` and `~/.hermes/oracle_wallet`)
- Embeddings (1536-d FLOAT64, must match `HERMES_AGENT_MEMORY.EMBEDDING`):
  1. OCI GenAI `cohere.embed-v4.0` via instance principal if enabled (`OCI_GENAI_REGION`, optional `OCI_COMPARTMENT_ID` / `OCI_EMBED_MODEL`)
  2. Else OpenRouter `openai/text-embedding-3-small` (`OPENROUTER_API_KEY`)
  First OCI 404/auth failure in a process skips OCI for the rest of that process.

## Setup

```bash
hermes config set memory.provider oracle
```

Then restart the gateway / start a new session.

## What it uses on ADB

- `VECTOR(1536, FLOAT64)` + IVF neighbor-partition index (`HERMES_AGENT_MEMORY_VEC_IDX`)
- Oracle Text `SEARCH INDEX` on `CONTENT` (`CONTAINS` / `SCORE`)
- Hybrid recall: VECTOR + Text `UNION ALL` in one SQL round-trip (no native HVI on this ADB)
- Writes insert text immediately; embeddings backfill in batches of 32
- Query embedding LRU cache (256)
- Prefetch wait 0.5s
- `DBMS_VECTOR.UTL_TO_CHUNKS` for long-document and file splits
- Hybrid rank: 0.7 * (1 - cosine distance) + 0.3 * normalized text score

## Tools

| Tool | Description |
|------|-------------|
| `oracle_search` | hybrid / vector / text recall |
| `oracle_add` | store a durable fact (embedded + indexed) |
| `oracle_update` | replace content by memory_id |
| `oracle_delete` | delete by memory_id |
| `oracle_chunk` | split text or disk file (`content` or `file_path`, supporting ShareGPT/JSON exports) with in-database UTL_TO_CHUNKS |

### Enhanced `oracle_chunk` Tool

The `oracle_chunk` tool supports direct file chunking via `file_path` in addition to inline `content`. When `file_path` is provided, the tool reads the file locally, automatically parses supported conversation export formats (ShareGPT, Claude, ChatGPT, or summary JSON) into structured turns, and executes in-database chunking using Oracle ADB's `DBMS_VECTOR.UTL_TO_CHUNKS`. This avoids piping large file payloads through the LLM prompt context.

**Parameters:**
- `file_path` *(string, optional)*: Absolute or relative path to a local text, markdown, or JSON file.
- `content` *(string, optional)*: Inline text content to chunk.
- `max_words` *(integer, default 80)*: Maximum words per chunk.
- `overlap` *(integer, default 10)*: Number of overlapping words between consecutive chunks.

**Tool Call Example:**
```json
{
  "file_path": "/path/to/conversations.json",
  "max_words": 1000,
  "overlap": 10
}
```

## Skills

### `oracle-import`

Located at [`skills/oracle-import/SKILL.md`](skills/oracle-import/SKILL.md), this skill enables bulk ingestion of historical conversation archives and large documents into Oracle Autonomous AI Database 26ai memory without hitting LLM token limits or upstream provider rate limits (such as NVIDIA NIM HTTP 429).

#### Supported Formats

| Format | Structure | Parsing Behavior |
|--------|-----------|------------------|
| **ShareGPT** | `{"conversations": [{"from": "...", "value": "..."}]}` | Turns extracted and prefixed with `[ROLE]` |
| **Claude Export** | `[{"name": "...", "summary": "...", "chat_messages": [...]}]` | Extracts titles, summaries, and user/assistant messages |
| **ChatGPT Export** | Standard ChatGPT multi-turn archive | Formats user and assistant turns with message roles |
| **Summary / Overview JSON** | `{"conversations": [...]}` or conversation summaries | Ingested as system/session memory turns |
| **Plain Text / Markdown** | `.txt`, `.md`, logs | Split recursively via in-database `UTL_TO_CHUNKS` |
| **Truncated JSON Streams** | Partial or aborted downloads | Recovers valid leading JSON objects without failing |

#### Ingestion Methods

1. **Via Hermes Agent (`oracle_chunk`)**:
   Agents can directly invoke the `oracle_chunk` tool passing `file_path`:
   ```bash
   oracle_chunk(file_path="/path/to/archive.json", max_words=500, overlap=20)
   ```

2. **Standalone Bulk Ingestion Script**:
   For large exports (>10MB or hundreds of sessions), run the bundled CLI script to import batches directly into Oracle ADB with zero LLM API consumption:
   ```bash
   # Import an archive
   python3 skills/oracle-import/scripts/import_conversations.py /path/to/oracle_import.json

   # Specify custom batch size and session ID
   python3 skills/oracle-import/scripts/import_conversations.py /path/to/conversations.json --batch-size 50 --session-id "archive-2026-06"
   ```

## Testing & Verification

Unit tests are isolated from external dependencies and live in `tests/`:

```bash
# Run tests with pytest
pytest

# Or run with Python's built-in unittest
python3 -m unittest discover tests
```

Tests use `tests/conftest.py` which provides lightweight mocks for Hermes host structures (`agent`, `tools`) and database drivers (`oracledb`, `oci`), enabling tests to run cleanly in CI/CD and automated agent environments without needing a live Oracle database.

## Jules AI Agent Integration (jules.google.com)

This repository is maintained and updated asynchronously with [Jules](https://jules.google.com) (`google-labs-jules[bot]`). Contributor guidelines and autonomous agent configuration instructions are defined in [`AGENTS.md`](./AGENTS.md).

### How to Message & Interact with Jules from GitHub

1. **Pull Requests:** When Jules opens a pull request, you can reply directly to any review comment or PR thread with feedback, requested modifications, or next steps. Jules will process your instructions asynchronously and push commits to the PR.
2. **Mentions & Commands:** You can message Jules in PR comments or issue discussions by mentioning `@jules` or `@google-labs-jules[bot]`.
3. **Issue Labeling:** Adding the `jules` label to any GitHub issue triggers Jules to analyze the issue, create a plan, and submit a pull request.
4. **Web Interface:** You can also dispatch tasks, track progress, and configure preferences (such as Reactive Mode) directly at [jules.google.com](https://jules.google.com).

## Packaging & Releases

Package metadata and dependencies are defined in `pyproject.toml` and `plugin.yaml`.

To build the wheel and source distribution:

```bash
# Using uv
uv build

# Or using python -m build
python3 -m build
```
