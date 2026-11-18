# Hermes Agent — Oracle 26ai Memory Provider

![Version](https://img.shields.io/badge/version-1.1.1.5-blue.svg)
![License](https://img.shields.io/badge/license-BSL--1.1-orange.svg)

> **CRITICALITY NOTICE**: Shared-corpus read path (oracle_load_skill), live sync on skill create/update (on_skill_lifecycle ingest), zombie-safe host-scoped pruning, idempotent init, separated ingest timeout.

A high-performance external memory provider plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent), backed by **Oracle Autonomous AI Database 26ai**. It gives the agent durable, cross-session memory with fused **vector + full-text hybrid recall**, in-database ONNX embeddings, background persistence, and a **shared fleet-wide skill corpus**.

## Install

> **CRITICALITY NOTICE**: Shared-corpus read path (oracle_load_skill), live sync on skill create/update (on_skill_lifecycle ingest), zombie-safe host-scoped pruning, idempotent init, separated ingest timeout.

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
python3 tests/verify_provider.py --env-file <path to your Hermes .env>
```

The script stores a few test memories, searches for them in each mode, and deletes them again. See [INSTALL.md](INSTALL.md#step-5-verification--testing) for its options.

## Configure

Connection secrets go in `~/.hermes/.env` — see the [installation guide](INSTALL.md) for the full walkthrough. Minimum required:

```bash
OCI_DB_USER="<your-db-user>"
OCI_DB_PASSWORD="..."
OCI_DB_DSN="(DESCRIPTION=(...))"        # Oracle Net descriptor or TNS alias
OCI_WALLET_LOCATION=~/.oci/wallet        # mTLS wallet dir (optional)
```

Embeddings are computed 100% in-house:

1. **In-database ONNX** (`HERMES_ORACLE_ONNX_MODEL=BGE_BASE_EN_V1_5`) — strictly in-database kernel execution; external fallback disabled.

### Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `OCI_DB_USER` | `<your-db-user>` | Database user name |
| `OCI_DB_PASSWORD` | - | Database user password |
| `OCI_DB_DSN` | - | Connection string / TNS descriptor |
| `OCI_WALLET_LOCATION` | - | mTLS client wallet directory (`~/.oci/wallet`) |
| `HERMES_HOST_ID` | System user | Unique host tag for shared-corpus skill entries |
| `HERMES_ORACLE_TABLE` | `HERMES_AGENT_MEMORY` | Memory table name |
| `HERMES_ORACLE_ONNX_MODEL` | `BGE_BASE_EN_V1_5` | In-database ONNX model name |
| `HERMES_ORACLE_SKILL_FETCH_MAX_CHUNKS` | `400` | Safety cap for chunk reassembly during `oracle_load_skill(fetch)` |
| `HERMES_ORACLE_INGEST_TIMEOUT` | `600` | Timeout in seconds for batch skill syncing and ingestion operations |
| `HERMES_ORACLE_SEARCH_TIMEOUT_MS` | `30000` | Timeout in milliseconds for interactive recall queries (read-path fail-fast clamp) |
| `HERMES_ORACLE_POOL_WAIT_TIMEOUT_MS` | `20000` | Max wait for a pooled connection before the call fails with `DPY-4005` (never blocks forever) |
| `HERMES_ORACLE_POOL_IDLE_TIMEOUT` | `300` | Seconds an idle pooled session above the pool minimum stays open |
| `HERMES_ORACLE_PREFETCH_WAIT` | `3` | Seconds a turn waits for background recall before continuing without it (0.1–7) |

## Tools

| Tool | Purpose |
|---|---|
| `oracle_search` | hybrid / vector / text recall; optional `tags` filter (`tag_match` `all`/`any`) |
| `oracle_add` | store a durable fact (embedded + indexed); optional stable `memory_id` (upsert) and `tags` |
| `oracle_get` | exact read by `memory_id`(s) and/or `tags` — no embedding, works during an ONNX cooldown |
| `oracle_update` | replace a fact's text and/or tags by `memory_id` |
| `oracle_delete` | delete by `memory_id`, or purge one thread by `session_id` (never skill rows or the shared `skills`/`none` ids). A purge takes `dry_run` to count first, and deletes at most 200 rows unless `max_rows` allows more (up to 10,000) |
| `oracle_chunk` | split inline text or a disk file in-database (refuses credential and system paths: env files, key directories and files, wallets, `/etc`) |
| `oracle_sync_skills` | scan, parse, and index skill libraries into vector memory |
| `oracle_load_skill` | load or list skills from the shared Oracle skill corpus at runtime |

### Handoffs between agents and sessions

Give a row a stable `memory_id` and tags, and any other agent or session sharing the database can pick it up by exact key — no semantic search needed:

```text
oracle_add(memory_id="handoff:billing:migration", tags=["handoff", "project:billing"],
           content="State: schema migrated on staging. Next: backfill invoices. Blocker: none.")
oracle_get(tags=["handoff", "project:billing"])          # receiver: newest first
oracle_update(memory_id="handoff:billing:migration", tags=["handoff", "project:billing", "done"])
```

Calling `oracle_add` again with the same `memory_id` updates the row in place (tags are kept unless you pass new ones). Tags are lower-cased; ids are 1–128 characters of letters, digits and `_ . : @ / -`.

## Skills

- **`oracle-skill-corpus`** — discover and materialize shared-corpus skills dynamically from Oracle ADB at runtime without local disk installation.
- **`oracle-import`** — bulk-ingest conversation archives (ShareGPT / Claude / ChatGPT / JSON) and large documents into ADB without LLM rate limits or context inflation.

## Shared Multi-Server Skill Corpus

The shared Oracle skill corpus turns Oracle Autonomous AI Database 26ai into a central, multi-server skill source of truth accessible across all Hermes instances. Any server profile can discover and reconstruct skills at runtime without requiring local disk presence.

### Storage Layout

Per ingested skill `S`:

| `memory_id` | Role | Description |
|---|---|---|
| `skill:{host}:{S}:c0` | `skill` | Meta row: `[skill_ingest] [skill:S] S — {description}\nWhen to use: {triggers}\nTags: {tags}` |
| `skill:{host}:{S}:c1 ... cN-1` | `skill` | Body chunks: `[skill_ingest] [skill:S] S (chunk i/N):\n{text}` |
| `skill:{host}:{S}:cN ...` | `skill` | Reference file chunks: `(reference: rel/path) (chunk i/M)` |

- **Metadata** (JSON): `category, chunk_index, hash (sha256 of source file), kind=skill_ingest, mtime, path, skill_name, total_chunks, version, host, target=memory`
- **Reconstruction fidelity**: Chunks are 80 whole words joined by single spaces with 10-word overlap. Reassembled text is word-accurate and lossless at word level (line/markdown formatting flattened).
- **Host identity**: `{host}` is `HERMES_HOST_ID`, or the short host name in lower case. A host only replaces or deletes rows under its own `skill:{host}:` prefix. Profiles on one host share the id and so share one copy per skill name. A copy whose SKILL.md is live outside the profile's skill dirs (its skills dir and `~/.agents/skills`, plus any `--dirs`) is not replaced: the bulk sync reports it as `skipped_foreign` with a hint, and `replace_foreign` (`--replace-foreign`) replaces it. Profiles are not fully isolated (#14), so set `HERMES_HOST_ID` per profile to keep them apart.
- **Backward compatibility**: Legacy single-row skills (pre-c0 era, no `:cN` suffix) are treated as meta rows and return with status `"meta_only"`. Legacy rows without a host (`skill:{S}:cN`) form the shared corpus; no host prunes them.

### Using `oracle_load_skill`

#### Listing / Discovering Skills (`action="list"`)

Search the catalog by substring or paginate:

```json
{
  "action": "list",
  "filter": "address",
  "offset": 0
}
```

Returns `{count_in_page, offset, more, skills: [{name, category, description, copies?}]}` — one entry per skill name; `copies` appears when several hosts store the skill.

#### Materializing a Skill (`action="fetch"`)

Fetch and reconstruct the full skill document:

```json
{
  "action": "fetch",
  "skill": "usps-address-validator"
}
```

Returns `{status: "ok", skill, category, source_host_path, total_chunks, chunk_count, word_count, markdown, references: {rel_path: text}, content_hash, mtime}`.
If several copies exist (different hosts or paths), returns `status: "multiple_variants"` with a `variants` list (`variant`, `host`, `path`, `hash`, `mtime`); pass `variant`, or `host` and/or `path`, to pick one.

### Host-Scoped Pruning

In a shared multi-server table, deletions are host-scoped:
- **Other hosts' rows and the host-less shared corpus are never touched.**
- Pruning is opt-in. It removes this host's skills whose `SKILL.md` is gone from the scanned roots (the active profile's skills dir and `~/.agents/skills`, or `--dirs`):
  ```bash
  python3 scripts/sync_skills.py --prune --dry-run      # preview
  python3 scripts/sync_skills.py --prune --prune-limit 500
  python3 scripts/sync_skills.py --prune-host /path/to/local/skills/   # only skills under this dir
  ```
  The `oracle_sync_skills` tool takes the same options (`prune`, `prune_limit`, `dry_run`, `replace_foreign`).
- Each changed skill is replaced in one transaction (delete of this host's rows + insert), so a failed write keeps the previous copy.

### Live Lifecycle Syncing

The `on_skill_lifecycle` hook synchronizes changes in real-time as skills are managed by the agent:
- The hook only schedules work. One background writer per skill looks up the file under the active profile's skill roots, checks the content hash and writes; events that arrive while it runs are coalesced into one re-run with the latest event.
- Actions `created`, `edited`, `patched`, `installed`, `restored` replace this host's copy of the skill (skipped when the hash is unchanged).
- Action `archived` deletes this host's copy only if no `SKILL.md` for the skill is left on disk.
- The file lookup walks the skill roots at any depth, as the bulk sync does: hidden trees are skipped, each real directory is entered once (link loops end), and a `SKILL.md` that resolves into an ignored tree such as `.archive/` is not live.

### Legacy Host-Identity Migration

Rows written before 1.1.1.4 have no host (`skill:{name}:cN`). The migration gives a legacy skill its owner's identity only when one host has a live `SKILL.md` at the stored path; everything else stays in the shared corpus. Upgrade every host first, then run it from one host:

```bash
# on each host: list its live skills (no database access)
python3 scripts/sync_skills.py --list-local > host-a.json
# from one host: dry run (default), review the counts, then apply
python3 scripts/sync_skills.py --migrate-host-identity --listing host-a.json --listing host-b.json
python3 scripts/sync_skills.py --migrate-host-identity --listing host-a.json --listing host-b.json --apply
# undo (dry run first, then --apply)
python3 scripts/sync_skills.py --reverse-host-identity
```

The migration runs in one transaction, checks the updated row count before committing, and keeps the old id in `metadata.legacy_id` for the reverse step.

## Vector Index Maintenance & ORA-51815

Under heavy write or bulk ingest workloads, queries against in-memory HNSW vector indexes may encounter:
```text
ORA-51815: INMEMORY NEIGHBOR GRAPH HNSW vector index snapshot is too old.
```
- **Transient & Self-Healing**: `ORA-51815` is a transient concurrency condition while the in-memory neighbor graph refreshes its snapshot. It needs **waiting or retrying**, NOT rebuilding.
- **Automatic Read Retry & Degradation**: Hybrid search and vector search automatically retry queries encountering `ORA-51815` with exponential backoff (3 attempts, 0.5s backoff). On exhausted retries, hybrid search gracefully degrades to Oracle Text search rather than failing.
- **Index Rebuild Helper (`scripts/rebuild_index.py`)**: Rebuilding a vector index is a last resort. Use the canonical helper:
  ```bash
  python3 scripts/rebuild_index.py --table HERMES_AGENT_MEMORY
  ```
  This drops and recreates the index using the canonical `VECTOR_INDEX_DDL` constant:
  ```sql
  CREATE VECTOR INDEX {index_name} ON {table_name} (embedding) ORGANIZATION INMEMORY NEIGHBOR GRAPH
  ```
  and automatically refreshes optimizer statistics via `DBMS_STATS.GATHER_TABLE_STATS`.

## What's new in v1.1.1.5

Follow-up release for #14 — full details in `RELEASE_NOTES_1.1.1.5.md`:

- **Foreign skill copies**: ownership is judged against the profile's own skill dirs, so a sync over a `--dirs` subset updates the copies it stored from the other dirs (a `--dirs` run outside the defaults never takes over a live default skill); `replace_foreign` / `--replace-foreign` replaces a copy left from another profile or a removed root, and the sync result carries a hint when it skipped one.
- **Lifecycle lookup**: a link into `.archive/` is no longer a live skill; the lookup walks any depth and ends on link loops, like the bulk sync.
- **Backfill**: no re-check or re-queue once shutdown has begun; a write whose per-row counts the driver cannot report is re-checked instead of dropped.
- **Test suite**: a runtime guard refuses any link outside the temp dir; the source scan also catches aliased and shell links.

## What's new in v1.1.1.4

Robustness release with per-host skill identity — full details in the `[1.1.1.4]` section of `CHANGELOG.md`:

- **Per-host skill copies**: skill rows are `skill:{host}:{name}:cN`; a host only replaces or deletes its own copy, pruning is opt-in, and a dry-run-first migration (with a reverse step) relabels existing rows.
- **Lifecycle hook off the caller's thread**: one writer per skill, coalesced events, profile-aware skill roots.
- **No lost writes during an ONNX cooldown**: facts are stored at once and embedded later by a backfill with backoff.
- **Pool and transaction safety**: old pools are retired, shutdown never closes a pool under a busy worker, inserts never retry inside a broken transaction, `ORA-00060` no longer starts the cooldown.
- **Faster, consistent recall**: CLOBs in one round trip, sized fetches, ranking before the `top_k` cut, strict timeout classification.
- **Tool limits**: batch and output caps, read-only skill rows for the generic tools, case-insensitive tags, memory mirror handles replace/remove.
- **Hygiene**: helper scripts read an env file only with `--env-file`; SPDX license `BUSL-1.1`.

## What's new in v1.1.1.3

Reliability + capability release (details preserved in `CHANGELOG.md`):

- **No more false "embedding model is unavailable"**: broken pooled sessions (`ORA-01002`), client timeouts, duplicate ids and other data errors, and Python exceptions no longer start the 60 s ONNX cooldown; poisoned connections are dropped and search retries once.
- **Skill-lifecycle hook survives provider shutdown** instead of failing every event with `pool not initialized`.
- **Bounded timeouts**: pool acquire waits at most 20 s (it could hang for minutes), `HERMES_ORACLE_SEARCH_TIMEOUT_MS` now really controls recall (default 30 s), idle sessions are kept 300 s, prefetch waits up to 3 s.
- **Handoff rows**: stable `memory_id` + `tags` on `oracle_add`, tag filters on `oracle_search`/`oracle_update`, and the new `oracle_get` exact-read tool.
- **Security and data safety**: `oracle_chunk(file_path=...)` refuses credential and system files (they would land in the shared database); `oracle_delete` can no longer purge the shared skill corpus; `oracle_sync_skills` validates its arguments (a string `dirs` no longer scans `/`).
- **Skill sync that actually runs**: the skill-lifecycle hook works inside Hermes (it failed silently on an import collision), archived skills are pruned, and a skill update replaces its rows in one transaction, so an interrupted update keeps the old rows instead of dropping the skill on every host.
- **Clean shutdown and recovery**: timed-out sessions are dropped instead of reused (`ORA-01002`), shutdown waits for skill writers and closes the pool once borrowed connections return, and a failed `initialize()` cleans up after itself.
- **Safe test suite**: tests no longer touch an installed plugin or `~/.hermes/skills`; CI runs them with a sandboxed `HOME`. Do not run the test suite of v1.1.1.2 or older on a host with a live install.

## What's new in v1.1.1.2

Maintenance release (details preserved in `CHANGELOG.md`):

- **Search-timeout default corrected**: `HERMES_ORACLE_SEARCH_TIMEOUT_MS` now defaults to `13000` (13 s), aligned 1:1 with the read-path clamp `_SEARCH_TIMEOUT_MS` (13.0 s) and the documented timeout budget — the previous `1300` (1.3 s) typo is fixed.
- **Single timeout read in `initialize()`**: the ingest/search timeout values are now read from the environment once and reused for both the pool fingerprint and the pool construction, so a mid-initialization env change can never split them into two divergent values.
- **Docs accuracy**: `INSTALL.md`'s ADB connection sample now uses the real TCP/TLS port (`1522`, not `1521`), and the README/INSTALL env tables name the variable the code actually reads (`HERMES_ORACLE_SEARCH_TIMEOUT_MS`, default `13000`).

## What's new in v1.1.1

Sterile distribution release (details preserved in `CHANGELOG.md`) — no provider behavior change:

- **Identifier scrub**: no fleet hostnames, operator/developer usernames, absolute home/build paths, or production OCI region + service descriptors anywhere in the shipped tree — the plugin installs cleanly on any host without leaking its origin.
- **Clean install scan**: the tree now clears the Hermes install-time security scanner with a `safe` verdict (0 critical, 0 high — previously a `caution` verdict that required a confirmation prompt on every install/update).

## What's new in v1.1.0.1

Maintenance release — fleet-sync hardening (full details in `CHANGELOG.md`):

- **Sync interpreter guard**: `scripts/sync_skills.py` re-executes into a venv that can import `oracledb` (import probe, loop-guarded) instead of dying with `No module named 'oracledb'` when launched from a bare interpreter.
- **Canonical skill meta-row ID**: meta sync rows now use `skill:{host}:{name}:c0` (matching the full-mode chunk-0 row), eliminating `ORA-00001` duplicates and restoring second-run idempotency.
- **Write-time budget**: pooled connection `call_timeout` defaults to `max(search, ingest)`; bulk ONNX inserts no longer die at the 13 s search default (`DPY-4024`). Read paths keep the tight 13 s window.
- **Transient-embedding fallback**: server-side transient embedding errors (`ORA-01013`/`ORA-04031`/…) now degrade to NULL-embedding insert + background backfill instead of a 60 s cooldown + lost row — across all three write ladders (`_insert`, `_insert_many`, `oracle_update`).
- **Pool fingerprint** now includes the timeout budget, so timeout-config changes rebuild the pool in long-lived processes.
- **Self-deadlock fix in live sync**: the sync's deletes are committed before the provider's `_insert_many` re-inserts the same rows on a second pool connection — the old order parked the run forever on its own uncommitted row locks.

## What's new in v1.1.0

- **Shared Cross-Server Skill Corpus (`oracle_load_skill`)**:
  - Full skill discovery and reconstruction path (`action="fetch"` / `action="list"`) allowing any Hermes host to discover and load any skill across the 2,300+ fleet-wide library dynamically at runtime, without local disk installation.
  - Word-accurate chunk reassembly reproducing source documents from 80-word, 10-word overlap chunks including bundled reference files.
  - Multi-variant resolution with explicit path and variant disambiguation.
- **Live Lifecycle Ingestion (`on_skill_lifecycle`)**:
  - In-process hook integration syncing skills into Oracle 26ai ADB immediately upon `created`, `edited`, `patched`, `installed`, or `restored` events.
  - SHA256 hash idempotency preventing redundant DB round-trips when skill contents are unchanged.
  - Safe retirement on `archived` events when local files are removed.
- **Zombie-Safe Host-Scoped Pruning (`scripts/sync_skills.py`)**:
  - Host-scoped pruning keyed on `(skill_name, metadata.path LIKE host_root)` pairs—foreign-host skills are strictly preserved, while locally deleted skills are cleaned up.
  - Host-tagged `memory_id` and `conv_ref` convention (`skill:{host}:{name}:cN`) preventing accidental sibling host overwrites.
  - Dead SQL leg 3 removed; one-shot migration utility (`--migrate-host-tags`).
- **Resilient & Idempotent Initialization**:
  - Idempotent `initialize()` preventing duplicate connection pools and eliminating `pool not initialized` warnings during multi-context instantiation.
  - Stale connection detection and automatic re-acquisition for ADB restart recovery.
  - Provider health diagnostic reporting (`health()`).
- **Separated Ingest Timeout**:
  - Autonomous ingestion timeout (`HERMES_ORACLE_INGEST_TIMEOUT`, default 600s) decoupled from query search timeouts (`HERMES_ORACLE_SEARCH_TIMEOUT_MS`, default 13000ms) to avoid batch timeouts during vector materialization.

## Contributing

Pull requests welcome. First-time contributors sign the [CLA](CLA.md) by commenting on the PR:

```text
I have read the CLA Document and I hereby sign the CLA
```

## License

[Business Source License 1.1](LICENSE) — free for personal, research, and organizations under $1M annual revenue; converts to Apache-2.0 on 2030-10-11.
