# Release Notes — Hermes Agent Oracle 26ai Memory Provider v1.0.9.4

> [!IMPORTANT]
> **CRITICALITY NOTICE**: v1.0.9.4 updates are CRITICAL to functionality. Recency & fact-prioritized search ranking, turn noise filtering (`role NOT IN ('turn', 'assistant', 'session')`), exact prefix match score boosting, fortified ONNX insert path (`ORA-51815` transient snapshot retry, `ORA-54401` conservative token clamp retry, background backfill queueing on null fallback), canonical root `VERSION` file, and 100% zero drift across repository, installed plugin, and all Hermes Agent profile environments.

### What's New in v1.0.9.4

#### 🎯 Re-Engineered Search Ranking & Turn Noise Filtering
- **Fact-First Role Prioritization**: Queries rank durable facts before generic context (`ORDER BY CASE WHEN role = 'fact' THEN 1 ELSE 2 END ASC, ..., created_at DESC`) across all text, vector, and hybrid search SQL queries.
- **Oracle Text Score Normalization**: Re-aligned text score scaling (`txt / 100.0`) to match Oracle Text score boundaries, improving composite hybrid ranking quality.
- **Exact Prefix Match Boosting**: Implemented `_is_prefix_match` with word-boundary and stop-word validation to detect query title/header matches (e.g. `# HANDOFF`, `**HANDOFF**:`, `[HANDOFF]`) across hybrid, text, and vector recall modes, applying a `+0.15` relevancy score boost.
- **Turn Noise Elimination**: Hardened `_JUNK` filter with `NVL(role, 'fact') NOT IN ('turn', 'assistant', 'session')`, strictly preventing assistant responses, session metadata, and conversation turns from polluting recall memory.

#### 🛡️ Fortified In-Database ONNX Insert & Update Paths
- **`ORA-51815` Transient Snapshot Conflict Retries**: Retries transient snapshot conflicts up to `_EMBED_MAX_RETRIES` with progressive backoff across `_insert`, `_insert_many`, and `oracle_update` before falling back.
- **`ORA-54401` Conservative Token Clamp Retry**: Automatically retries sequence length violations with a conservative token clamp (`_CONSERVATIVE_EMBED_CHARS = 400`), mathematically guaranteeing input stays strictly under 512 tokens across `_insert`, `_insert_many`, `oracle_update`, and `_embed_many_tagged`.
- **Asynchronous Embed Backfill on Fallback**: Fallback inserts and updates with NULL embeddings automatically enqueue rows into `_queue_embed_backfill`, ensuring vector embeddings are computed asynchronously in the background with zero turn loss.

#### 🧪 Comprehensive Test Coverage & Live Verification
- **New Unit Tests**: Added test coverage in `tests/test_oracle.py` for ORA-54401 conservative token clamp retry and success, ORA-51815 transient snapshot conflict retry, oracle_update fortification, vector search prefix boosting, and backfill queueing on null fallback across single and bulk insertions.
- **Live Search & Filter Verification**: Automated validation in `scripts/verify_provider.py` confirming ranking boosts and complete turn exclusion.

#### 🔄 Complete Zero-Drift Synchronization
- **Zero Drift Enforcement**: 100% byte-for-byte synchronization across the canonical repository, `~/.hermes/plugins/oracle`, and all bot profile environments (`edgebot`, `qoam-recon`, `qoam-backend`, `qoam-release`, `qoam-safeguard`, `qoam-ui`, `qoam-watcher`, `qoam-orchestrator`).
- **Core File Isolation**: Zero modifications made to Hermes Agent core repository files.
