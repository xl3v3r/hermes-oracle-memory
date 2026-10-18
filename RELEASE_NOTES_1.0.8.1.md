# Release Notes — Hermes Agent Oracle 26ai Memory Provider v1.0.8.1

> [!IMPORTANT]
> **CRITICALITY NOTICE**: v1.0.8.1 updates are CRITICAL to functionality. Updated 9 Oracle 26ai system prompt memory rules, complete elimination of local memory conflation, and zero-drift deployment require this version.

### What's New in v1.0.8.1

#### 🧠 Updated 9 Oracle 26ai Memory Rules
Updated system prompt body (`_PROMPT_BODY`) and `system_prompt_block()` with the definitive 9 Oracle 26ai memory rules:
1. **ORACLE-FIRST SEARCH**: Before answering any context-dependent question, call `oracle_search`. Use results as authoritative context. Re-search with broader terms if ambiguous. If Oracle is unreachable, state that no memory is available. Ask the user only if Oracle returns nothing.
2. **ORACLE-FIRST FACT SINK**: All lasting facts, preferences, decisions, configurations, and learnings go to Oracle via `oracle_add`. Use a stable memory_id, compact content (<~200 chars), and tags. No local fallback — if Oracle is unreachable, facts are not stored.
3. **MEMORY_ID LIFECYCLE**: `oracle_search` before `oracle_add` to avoid duplicates. If memory_id exists, use `oracle_update(memory_id, ...)`. Use `oracle_delete(memory_id)` for obsolete or incorrect facts.
4. **STORAGE TIERS**: `oracle_add` for atomic facts/batch lists; `oracle_chunk` for documents/logs/conversation exports; `skill_manage` for task-specific procedures; no TODO state stored anywhere.
5. **ORACLE-ONLY STORE**: There is no local memory. `MEMORY.md` and all local files are off-limits for facts. All lasting facts live exclusively in Oracle 26ai.
6. **ORACLE UNAVAILABILITY**: If Oracle is unreachable, the agent remembers nothing. No facts are buffered, cached, or written locally. State clearly that Oracle is unavailable. On reconnection, resume normal operations.
7. **INTERNAL WRITE-AHEAD**: When Oracle is reachable, stage writes inside Oracle before committing. `oracle_add` to a staging memory_id prefixed with `wal_` (e.g., `wal_20261017_user_lang`) with tags `["pending", "wal"]`. Validate via `oracle_search`, then `oracle_add` the final entry with its canonical memory_id and production tags, and `oracle_delete` the `wal_` staging entry.
8. **SESSION SCOPING**: Tag every Oracle entry: `["global"]`, `["user:<id>"]`, `["session:<id>"]`, `["project:<name>"]`, `["session_learn"]`, or `["pending", "wal"]` for staging.
9. **VALIDATION BEFORE WRITE**: Confirm durability, dedupe via search, keep content compact, normalize terminology.

#### 🚫 Strict Local Memory Deactivation & Fallback Alignment
- Aligned `system_prompt_block()` unavailable fallback with Rule 6: `# Oracle 26ai Memory\nUnavailable ({reason}). No memory store is available. Local memory is prohibited.`.
- Built-in local file memory (`MEMORY.md`, `USER.md`) disabled in favor of Oracle Autonomous AI Database 26ai as sole system of record.
