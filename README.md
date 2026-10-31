# Hermes Agent — Oracle 26ai Memory Provider

![Version](https://img.shields.io/badge/version-1.0.9.4.2-blue.svg)
![License](https://img.shields.io/badge/license-BSL--1.1-orange.svg)

> **CRITICALITY NOTICE**: v1.0.9.4.2 updates are CRITICAL to functionality. Skill-path remediation (single-source telemetry SQL filter unblocking skill_ingest recall, isolated dataclass module loader for in-process oracle_sync_skills), generic multi-profile synchronization, and zero drift across repository, installed plugin, and all Hermes Agent profile environments.

A high-performance external memory provider plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent), backed by **Oracle Autonomous AI Database 26ai**. It gives the agent durable, cross-session memory with fused **vector + full-text hybrid recall**, in-database ONNX embeddings, and background persistence — a scalable replacement for local SQLite / Mnemosyne-style memory.

## Install

> **CRITICALITY NOTICE**: v1.0.9.4.2 updates are CRITICAL to functionality. Skill-path remediation (single-source telemetry SQL filter unblocking skill_ingest recall, isolated dataclass module loader for in-process oracle_sync_skills), generic multi-profile synchronization, and zero drift across repository, installed plugin, and all Hermes Agent profile environments.

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

## What's new in v1.0.9.4.2

- **Criticality Notice**: v1.0.9.4.2 updates are CRITICAL to functionality. Delivers skill-path remediation across retrieval and ingest, generic multi-profile synchronization, and strict release hygiene.
- **Skill Ingest Recall Unblocked (Defect 1)**:
  - Replaced overly greedy `[skill%` SQL filter with single-source `_telemetry_sql()` generator.
  - Excludes only lifecycle telemetry records (`[skill_usage]`, `[skill_vectorization*]`) with properly escaped SQL wildcards, restoring full recall visibility for `[skill_ingest]` indexed skills across hybrid, vector, and text search modes.
- **In-Process Skill Sync Fixed (Defect 2)**:
  - Added `_load_module_from_path` helper that pre-registers the imported module in `sys.modules` before executing bytecode.
  - Resolves CPython `@dataclass` introspection failure (`'NoneType' object has no attribute '__dict__'`), restoring in-process `oracle_sync_skills` tool invocations.
- **Generic Profile Synchronization**:
  - Dynamically discovers all Hermes Agent profile plugin directories under `Path.home() / ".hermes" / "profiles"` without hardcoding specific bot names.
  - Ensures 100% zero drift across canonical repository, installed plugin (`~/.hermes/plugins/oracle`), and all profile environments.
- **Release Hygiene**:
  - Maintained single release notes file `RELEASE_NOTES_1.0.9.4.2.md` and purged previous version release notes from git tracking and sync destinations.


## Contributing

Pull requests welcome. First-time contributors sign the [CLA](CLA.md) by commenting on the PR:

```text
I have read the CLA Document and I hereby sign the CLA
```

## License

[Business Source License 1.1](LICENSE) — free for personal, research, and organizations under $1M annual revenue; converts to Apache-2.0 on 2030-10-11.
