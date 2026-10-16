# Release Notes — Hermes Agent Oracle 26ai Memory Provider v1.0.7.1

> [!IMPORTANT]
> **CRITICALITY NOTICE**: v1.0.7.1 updates are CRITICAL to functionality. Oracle-first memory behavior, anti-conflation enforcement, and batch writes require this version.

### What's New in v1.0.7.1

#### 🧠 Oracle-First System Prompt & Dynamic Enforcement
- **Injected Header**: `# Oracle 26ai Memory (Primary Store)` explicitly designates Oracle 26ai ADB as the primary unbounded knowledge store.
- **Rule 1 — ORACLE-FIRST SEARCH**: Agents are instructed to always consult `oracle_search` first before answering queries dependent on prior context, user preferences, configurations, architecture, credentials, or project history.
- **Rule 2 — ORACLE-FIRST FACT SINK**: `oracle_add` is established as the primary sink for all lasting facts, user details, decisions, preferences, and session learnings, emphasizing unlimited cross-session persistence.
- **Rule 3 — ANTI-CONFLATION & NO CROSS-STORE DELETION**: Built-in `MEMORY.md` is strictly defined as a 1,600-character bootstrap buffer. Agents are explicitly forbidden from saving general facts or batch summaries to local memory, and forbidden from passing Oracle memory identifiers (`chk_*`, `fact_*`) to local memory edit or delete tools.
- **Rule 4 — BATCH & DOCUMENT STORAGE**: Directs atomic knowledge to `oracle_add` and larger documents, logs, and conversation archives to `oracle_chunk`.

#### 📦 Batch Fact Ingestion via `oracle_add`
- **Multi-Fact Payloads**: `oracle_add` now natively accepts `facts: list[str] | str` as well as `content: list[str] | str`.
- **Single Tool Call Atomicity**: Stores multiple facts in a single tool call with unique `fact_*` identifiers, returning `{"result": "Batch facts stored (N).", "memory_ids": [...], "count": N}`.

#### 🛡️ Tool Schema Anti-Conflation
- **Explicit Storage Boundaries**: Schemas for `oracle_search`, `oracle_add`, `oracle_update`, and `oracle_delete` instruct the LLM never to conflate local 1,600-char `MEMORY.md` with Oracle 26ai or use local memory tools for Oracle entries.

#### ⚡ ADB Service Tier Migration & Connection Pool Optimization
- **`_high` → `_low` Service Tier Migration**: SessionPool connection parameters tuned to conservative limits (`min=1, max=3, increment=1, threaded=True, getmode=oracledb.POOL_GETMODE_WAIT, timeout=5, wait_timeout=5`) to prevent Exadata auto-DOP parallel execution stalls and ORA-04036 PGA limit errors.
- **Case-Insensitive DSN Auto-Normalization**: Any DSN specifying `_high` (standalone, TNS alias, or full connect descriptor) is automatically normalized to `_low` case-insensitively upon pool initialization.

#### 🌐 OS-Agnostic Drop-In Design & Zero Drift
- **Distro & Path Agnostic**: Removed all hardcoded distribution or user paths (`/home/rocky/`); paths now resolve dynamically via `Path.home()` and relative workspace conventions.
- **Sole Source of Truth**: `xl3v3r/hermes-oracle-memory` is maintained as the single source of truth, with 100% byte-for-byte parity reconciled with the active Hermes plugin directory (`~/.hermes/plugins/oracle`).
- **Clean Metadata Pins**: Deprecated commit pins cleared from `.install-metadata.json`.
