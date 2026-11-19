# Hermes Agent — Oracle 26ai Memory Provider

![Version](https://img.shields.io/badge/version-1.1.2-blue.svg)
![License](https://img.shields.io/badge/license-BUSL--1.1-orange.svg)

A high-performance external memory provider plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent), backed by **Oracle Autonomous AI Database 26ai**. It gives the agent durable, cross-session memory with fused **vector + full-text hybrid recall**, in-database ONNX embeddings, background persistence, and a **shared fleet-wide skill corpus**.

## Install

```bash
hermes plugins install xl3v3r/hermes-oracle-memory --yes-deps --enable
hermes memory setup              # pick "oracle" from the list
```

Then restart Hermes, and the gateway if it runs as a service. A running process does not pick up a newly installed dependency.

`--yes-deps` accepts the plugin's Python dependency (`oracledb`) and `--enable` enables the plugin; leave them out to be asked. Hermes installs the dependency into the environment it loads the plugin from. Do not install it with `pip` yourself: `pip` is whatever is first on your `PATH`, which is usually another Python, and the provider then reports "oracledb is not importable in this Hermes process".

If you cloned the repository into the plugins directory instead (`$HERMES_HOME/plugins/oracle`), run `hermes plugins enable oracle`. It provisions the dependency in the same way. Then select the provider and restart:

```bash
hermes plugins enable oracle
hermes config set memory.provider oracle
```

### Verify

```bash
hermes memory status             # confirm "oracle" is active
python3 tests/verify_provider.py --env-file <path to your Hermes .env>
```

The script stores a few test memories, searches for them in each mode, and deletes them again. See [INSTALL.md](INSTALL.md#step-5-verification--testing) for its options.

## Configure

Connection secrets go in `$HERMES_HOME/.env` (by default `~/.hermes/.env`; a profile or a service install has its own home) — see the [installation guide](INSTALL.md) for the full walkthrough. Minimum required:

```bash
OCI_DB_USER="<your-db-user>"
OCI_DB_PASSWORD="..."
OCI_DB_DSN="(DESCRIPTION=(...))"        # Oracle Net descriptor or TNS alias
OCI_WALLET_LOCATION=~/.oci/wallet        # mTLS wallet dir (optional)
```

Embeddings are computed inside the database, by the ONNX model that `HERMES_ORACLE_ONNX_MODEL` names (default `BGE_BASE_EN_V1_5`). No text is sent to an embedding service, and there is no fallback provider: while the model is unavailable, rows are stored without an embedding and embedded later.

### Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `OCI_DB_USER` | - (required) | Database user name |
| `OCI_DB_PASSWORD` | - (required) | Database user password |
| `OCI_DB_DSN` | - (required) | Connection string / TNS descriptor |
| `OCI_WALLET_LOCATION` | - | mTLS client wallet directory. When unset: `TNS_ADMIN`, then `~/.oci/wallet`, then `$HERMES_HOME/oracle_wallet` |
| `OCI_WALLET_PASSWORD` | - | Password of the wallet, when it has one |
| `TNS_ADMIN` | - | Wallet / `tnsnames.ora` directory, used when `OCI_WALLET_LOCATION` is unset |
| `HERMES_HOST_ID` | short host name, lower case | Host tag in shared-corpus skill ids (`skill:{host}:{name}:cN`) |
| `HERMES_ORACLE_TABLE` | `HERMES_AGENT_MEMORY` | Memory table name |
| `HERMES_ORACLE_SCHEMA_POLICY` | - | `REQUIRE_EXISTING`: run no DDL at start-up, only check the schema — see [INSTALL.md](INSTALL.md#step-7-security--least-privilege-operation) |
| `HERMES_ORACLE_ONNX_MODEL` | `BGE_BASE_EN_V1_5` | In-database ONNX model name |
| `HERMES_ORACLE_SKILL_FETCH_MAX_CHUNKS` | `400` | Safety cap for chunk reassembly during `oracle_load_skill(fetch)` |
| `HERMES_ORACLE_SYNC_SKILLS_TOOL` | - | `1`, `true`, `yes` or `on`: offer the `oracle_sync_skills` tool to this profile's sessions (never to a user-scoped session) |
| `HERMES_ORACLE_INGEST_TIMEOUT` | `600` | Timeout in seconds for batch skill syncing and ingestion operations |
| `HERMES_ORACLE_SEARCH_TIMEOUT_MS` | `30000` | Timeout in milliseconds for interactive recall queries (read-path fail-fast clamp) |
| `HERMES_ORACLE_POOL_WAIT_TIMEOUT_MS` | `20000` | Max wait for a pooled connection before the call fails with `DPY-4005` (never blocks forever) |
| `HERMES_ORACLE_POOL_IDLE_TIMEOUT` | `300` | Seconds an idle pooled session above the pool minimum stays open |
| `HERMES_ORACLE_PREFETCH_WAIT` | `3` | Seconds a turn waits for background recall before continuing without it (0.1–7) |
| `HERMES_ORACLE_SCOPE_MODE` | `off` | `off`, `auto` or `required`: keep each caller's memory apart — see [Per-user scope](#per-user-scope) |
| `HERMES_ORACLE_MIN_CANDIDATE_POOL` | `50` | Rows each search fetches before ranking (at least this many, whatever `top_k` is) |
| `HERMES_ORACLE_MAX_CHUNKS_PER_SKILL` | `2` | Most chunks of one skill or imported conversation in a result list; `0` = no cap |
| `HERMES_ORACLE_RECENCY_FLOOR` | `0.85` | Score multiplier of a very old row (0–1). A new row has 1.0; `1` turns recency weighting off |
| `HERMES_ORACLE_RECENCY_SCALE_DAYS` | `120` | Age in days at which the recency bonus has decayed to about a third |
| `HERMES_ORACLE_TELEMETRY_WEIGHT` | `0.05` | Score multiplier of skill-usage and vectorization log rows (0–1) |
| `HERMES_ORACLE_DISABLE_RERANK` | - | `1`, `true` or `yes`: skip the `DBMS_VECTOR.RERANK` step of hybrid search |

### Per-user scope

By default (`off`) every session that uses the table reads and writes the same rows: recall is global. That suits one operator. It does not suit a frontend that serves several people (Open WebUI and the like), where one user's facts would be recalled into another user's chat.

`HERMES_ORACLE_SCOPE_MODE` binds each session to a scope when it starts:

| Mode | Session has an identity | Session has none |
|---|---|---|
| `off` | no scoping | no scoping |
| `auto` | the caller's own rows | the operator's rows (`global`) |
| `required` | the caller's own rows | the operator's rows on `cli`, `tui` and `cron`; on any other platform the provider refuses to start |

The identity is what Hermes passes to the provider: the gateway session key (for the API server, the `X-Hermes-Session-Key` header), or else platform and user id. It is never taken from a tool argument or from the database user. A key with an empty user part (`owui:`) counts as none. A forked agent takes the scope of its parent session.

In a scoped session:

- Search, recall, `oracle_get`, `oracle_update` and `oracle_delete` touch only the caller's rows. Rows written before scoping, and rows of sessions without an identity, belong to `global` and are not visible to a user.
- The skill corpus stays shared and read-only: every scope can search and load skills.
- The same `memory_id` can be used by two users; each gets a row of their own. Ids are limited to 109 characters there.
- `oracle_sync_skills` and `oracle_chunk(file_path=...)` are not offered to a user's session (both read this host's disk), and conversation turns are not captured automatically.
- A tool call from a caller other than the one the session was bound for is refused.

The mode needs the column `scope_id`. The provider adds it (and an index) at start-up; under `HERMES_ORACLE_SCHEMA_POLICY=REQUIRE_EXISTING` it stops with an error that names the statement to run:

```sql
ALTER TABLE HERMES_AGENT_MEMORY ADD (scope_id VARCHAR2(300) DEFAULT 'global' NOT NULL);
CREATE INDEX HERMES_AGENT_MEMORY_SCOPE_IDX ON HERMES_AGENT_MEMORY (scope_id);
```

Limits: scoping separates the users of a frontend. It does not separate agents or profiles that share one identity, and it is not a database security boundary — every session connects as the same database user.

## Tools

| Tool | Purpose |
|---|---|
| `oracle_search` | hybrid / vector / text recall; optional `tags` filter (`tag_match` `all`/`any`) |
| `oracle_add` | store a durable fact (embedded + indexed); optional stable `memory_id` (upsert) and `tags` |
| `oracle_get` | exact read by `memory_id`(s) and/or `tags` — no embedding, works during an ONNX cooldown |
| `oracle_update` | replace a fact's text and/or tags by `memory_id` |
| `oracle_delete` | delete by `memory_id`, or purge one thread by `session_id` (never skill rows or the shared `skills`/`none` ids). A purge takes `dry_run` to count first, and deletes at most 200 rows unless `max_rows` allows more (up to 10,000) |
| `oracle_chunk` | split inline text or a disk file in-database (refuses credential and system paths: env files, key directories and files, wallets, `/etc`) |
| `oracle_sync_skills` | scan, parse, and index skill libraries into vector memory. Offered only when the profile sets `HERMES_ORACLE_SYNC_SKILLS_TOOL=1`; `scripts/sync_skills.py` does the same from a shell |
| `oracle_load_skill` | search, list or load skills from the shared Oracle skill corpus at runtime |

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

#### Finding Skills (`action="search"`)

Describe the task:

```json
{
  "action": "search",
  "query": "validate a US postal address"
}
```

Returns `{count, skills: [{name, description, score}]}`: up to eight skills, best match first, one entry per skill name. The search reads only each skill's meta row (name, description, triggers).

Automatic recall uses the same rows. A turn's recalled context lists up to six memories and, under their own heading, up to three skills as `<retrieved_skill name="..." score="...">description</retrieved_skill>`. Skill body chunks are not recalled, and a skill installed in the profile (its `skills/` directory or `~/.agents/skills`) is not listed, since Hermes already shows it in its skill index.

#### Listing Skills (`action="list"`)

Filter the catalog by substring, or page through it (50 names per page):

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
  The `oracle_sync_skills` tool takes the same options (`prune`, `prune_limit`, `dry_run`, `replace_foreign`). A session has the tool only when the profile sets `HERMES_ORACLE_SYNC_SKILLS_TOOL=1`.
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

## Release history

The current release is v1.1.2. [CHANGELOG.md](CHANGELOG.md) lists the changes of every release; [RELEASE_NOTES_1.1.2.md](RELEASE_NOTES_1.1.2.md) has the upgrade notes for the current one.

## Contributing

Pull requests welcome. First-time contributors sign the [CLA](CLA.md) by commenting on the PR:

```text
I have read the CLA Document and I hereby sign the CLA
```

## License

[Business Source License 1.1](LICENSE) — free for personal, research, and organizations under $1M annual revenue; converts to Apache-2.0 on 2030-10-11.
