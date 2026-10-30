# Hermes Agent — Oracle 26ai Memory Provider

![Version](https://img.shields.io/badge/version-1.0.9.4.1-blue.svg)
![License](https://img.shields.io/badge/license-BSL--1.1-orange.svg)

> **CRITICALITY NOTICE**: v1.0.9.4.1 updates are CRITICAL to functionality. Recency & fact-prioritized search ranking with exponential decay, telemetry filtering, fortified ONNX insert path (`ORA-51815` transient snapshot retry, `ORA-54401` conservative token clamp retry, background backfill queueing on null fallback), canonical root `VERSION` file, and zero drift across repository, installed plugin, and all Hermes Agent profile environments.

A high-performance external memory provider plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent), backed by **Oracle Autonomous AI Database 26ai**. It gives the agent durable, cross-session memory with fused **vector + full-text hybrid recall**, in-database ONNX embeddings, and background persistence — a scalable replacement for local SQLite / Mnemosyne-style memory.

## Install

> **CRITICALITY NOTICE**: v1.0.9.4.1 updates are CRITICAL to functionality. Recency & fact-prioritized search ranking with exponential decay, telemetry filtering, fortified ONNX insert path (`ORA-51815` transient snapshot retry, `ORA-54401` conservative token clamp retry, background backfill queueing on null fallback), canonical root `VERSION` file, and zero drift across repository, installed plugin, and all Hermes Agent profile environments.

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

## What's new in v1.0.9.4.1

- **Criticality Notice**: v1.0.9.4.1 updates are CRITICAL to functionality. Introduces dynamic retrieval weighting (`_apply_weights`) incorporating exponential recency decay, candidate telemetry filtration (`[skill%`), role weighting, short content length penalties, and test harness self-execution guards against `SameFileError`. Enforces 100% zero drift across repository, installed plugin, and all Hermes Agent profile environments.
- **Dynamic Retrieval Weighting (`_apply_weights`)**:
  - Incorporates exponential recency decay (`0.85 + 0.15 * exp(-age_days / 120)`), smoothly prioritizing current facts over stale records while guaranteeing an 0.85 floor for older foundational memories.
  - Multiplies scores by candidate role types (`fact: 1.0`, `user: 0.9`, `session: 0.9`, `turn: 0.75`, `assistant: 0.75`).
  - Penalizes lifecycle telemetry (`[skill_usage]`, `[skill_vectorization*]`) with a 0.05 multiplier and eliminates telemetry from candidate sets via `_JUNK` filter (`AND NVL(content_txt, 'x') NOT LIKE '[skill%'`).
  - Applies short content dampening (`< 25 chars -> 0.9`) to low-information fragments.
  - Resolves `created_at` timestamps via batch PK lookup chunked at 500 items, with seamless unweighted fallback on any database exception.
- **Test Harness Hardening & Zero Drift**:
  - Guards against `repo_dir.resolve() == plugin_dir.resolve()` in `tests/test_oracle.py`, preventing `shutil.SameFileError` during in-place test runs.
  - Clean release notes in `RELEASE_NOTES_1.0.9.4.1.md` and updated `CHANGELOG.md`.
  - Zero modifications to Hermes Agent core repository files.


## Contributing

Pull requests welcome. First-time contributors sign the [CLA](CLA.md) by commenting on the PR:

```text
I have read the CLA Document and I hereby sign the CLA
```

## License

[Business Source License 1.1](LICENSE) — free for personal, research, and organizations under $1M annual revenue; converts to Apache-2.0 on 2030-10-11.
