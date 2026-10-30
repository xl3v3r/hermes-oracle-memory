# Hermes Agent — Oracle 26ai Memory Provider

![Version](https://img.shields.io/badge/version-1.0.9.4-blue.svg)
![License](https://img.shields.io/badge/license-BSL--1.1-orange.svg)

> **CRITICALITY NOTICE**: v1.0.9.4 updates are CRITICAL to functionality. Recency & fact-prioritized search ranking, turn noise filtering (`role NOT IN ('turn', 'assistant', 'session')`), prefix match score boosting, fortified ONNX insert path (`ORA-51815` transient snapshot retry, `ORA-54401` conservative token clamp retry, background backfill queueing on null fallback), canonical root `VERSION` file, and zero drift across repository, installed plugin, and all Hermes Agent profile environments.

A high-performance external memory provider plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent), backed by **Oracle Autonomous AI Database 26ai**. It gives the agent durable, cross-session memory with fused **vector + full-text hybrid recall**, in-database ONNX embeddings, and background persistence — a scalable replacement for local SQLite / Mnemosyne-style memory.

## Install

> **CRITICALITY NOTICE**: v1.0.9.4 updates are CRITICAL to functionality. Recency & fact-prioritized search ranking, turn noise filtering (`role NOT IN ('turn', 'assistant', 'session')`), prefix match score boosting, fortified ONNX insert path (`ORA-51815` transient snapshot retry, `ORA-54401` conservative token clamp retry, background backfill queueing on null fallback), canonical root `VERSION` file, and zero drift across repository, installed plugin, and all Hermes Agent profile environments.

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
| `oracle_sync_skills` | scan, parse, and index skill libraries (`~/.agents/skills`, `~/.hermes/skills`) into vector memory |

## Skills

- **`oracle-import`** — bulk-ingest conversation archives (ShareGPT / Claude / ChatGPT / JSON) and large documents into ADB without LLM rate limits or context inflation.

## What's new in v1.0.9.4

- **Criticality Notice**: v1.0.9.4 updates are CRITICAL to functionality. Introduces recency and fact-prioritized search ranking, turn noise filtering, prefix match score boosting, fortified in-database ONNX insert error handling with snapshot conflict retry (`ORA-51815`) and conservative token clamp retry (`ORA-54401`), background backfill queueing on null fallback, and enforces 100% zero drift across repository, installed plugin, and all Hermes Agent profile environments.
- **Search Ranking & Turn Noise Filtering**:
  - Implements fact-first role ordering (`ORDER BY CASE WHEN role = 'fact' THEN 1 ELSE 2 END ASC, ..., created_at DESC`) across all text, vector, and hybrid search SQL queries.
  - Normalizes text scores against standard Oracle Text max score (`min(1.0, txt / 100.0)`).
  - Implements exact prefix match detection (`_is_prefix_match`) granting a `+0.15` relevancy score boost to memories whose titles/prefixes match search queries.
  - Filters out turn noise (`turn`, `assistant`, `session`) from memory recall via `_JUNK` filter.
- **Fortified ONNX Insertion Paths (`_insert` and `_insert_many`)**:
  - **`ORA-51815` Transient Snapshot Retry**: Retries transient snapshot conflicts up to `_EMBED_MAX_RETRIES` with progressive backoff before falling back.
  - **`ORA-54401` Conservative Token Clamp**: Automatically retries sequence length violations with conservative character clamp (`_CONSERVATIVE_EMBED_CHARS = 400`), safely bounding tokens below 512.
  - **Background Backfill on NULL Fallback**: Any fallback insert with NULL embeddings automatically enqueues rows into `_queue_embed_backfill` to ensure vectors are computed asynchronously in the background without turn loss.
- **Repository Hygiene & Clean Release Documentation**:
  - Clean release notes in `RELEASE_NOTES_1.0.9.4.md` and updated `CHANGELOG.md`.
  - Zero modifications to Hermes Agent core repository files.


## Contributing

Pull requests welcome. First-time contributors sign the [CLA](CLA.md) by commenting on the PR:

```text
I have read the CLA Document and I hereby sign the CLA
```

## License

[Business Source License 1.1](LICENSE) — free for personal, research, and organizations under $1M annual revenue; converts to Apache-2.0 on 2030-10-11.
