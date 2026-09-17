# Oracle 26ai Memory Provider

Primary Hermes memory backend on Oracle Autonomous AI Database (VECTOR + Oracle Text).

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
