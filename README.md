# Oracle 26ai Memory Provider

![Version](https://img.shields.io/badge/version-1.0.3-blue.svg)

## About

`hermes-oracle-memory` is a high-performance external memory provider plugin for [Hermes Agent](https://github.com/nousresearch/hermes-agent), leveraging Oracle Autonomous AI Database (ADB) 26ai. It provides hybrid vector and full-text memory retrieval across conversations, enabling low-latency, scalable, and durable long-term memory for AI agents without relying on local SQLite or legacy memory systems like Mnemosyne.

## Requirements

- `oracledb` (thin mode)
- Env in `$HERMES_HOME/.env`: `OCI_DB_USER`, `OCI_DB_PASSWORD`, `OCI_DB_DSN`
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
- `DBMS_VECTOR.UTL_TO_CHUNKS` for long-document splits
- Hybrid rank: 0.7 * (1 - cosine distance) + 0.3 * normalized text score

## Tools

| Tool | Description |
|------|-------------|
| `oracle_search` | hybrid / vector / text recall |
| `oracle_add` | store a durable fact (embedded + indexed) |
| `oracle_update` | replace content by memory_id |
| `oracle_delete` | delete by memory_id |
| `oracle_chunk` | split text with in-database UTL_TO_CHUNKS |

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
