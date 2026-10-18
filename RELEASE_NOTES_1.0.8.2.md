# Release Notes — Hermes Agent Oracle 26ai Memory Provider v1.0.8.2

> [!IMPORTANT]
> **CRITICALITY NOTICE**: v1.0.8.2 updates are CRITICAL to functionality. Eliminates external OpenRouter fallback for strict 100% in-house database ONNX embeddings, tunes connection pool limits (`min: 2, max: 6, call_timeout: 30000`), fixes hyphen minus syntax collisions in Oracle Text queries, and registers `oracle_search` as parallel-safe.

### What's New in v1.0.8.2

#### 🔒 100% In-House Data Retention (Zero External Fallback)
- Completely excised the external OpenRouter embedding fallback mechanism (`_embed_openrouter`) from the memory plugin.
- All embedding computations are executed strictly within Oracle Autonomous AI Database 26ai using native in-database ONNX inference (`VECTOR_EMBEDDING` with `BGE_BASE_EN_V1_5`).
- Guaranteed 100% data enclave boundary: confidential facts, prompts, and memory chunks never leave the database or transit over third-party embedding APIs.
- If in-database ONNX inference fails or enters cooldown, operations (`_insert`, `_insert_many`, `_search`, `oracle_update`) fail-fast by raising descriptive `RuntimeError` exceptions rather than silently degrading to external network calls.

#### ⚡ Session Pool Concurrency Tuning
- Tuned `oracledb.SessionPool` allocation to `min: 2, max: 6` with `call_timeout: 30000` ms.
- Prevents connection starvation, pool exhaustion, and lock contention during concurrent Hermes turn synchronization and parallel tool calling.

#### 🛡️ Oracle Text Query Syntax Resilience (DRG-50901 Fix)
- Sanitized hyphen/minus characters in `_CONTAINS_CLEAN_RE = re.compile(r"[^\w\s]")`.
- In Oracle Text `CONTAINS()` queries, unescaped hyphens are evaluated as the `MINUS` operator, which triggered `DRG-50901: text query syntax error` when queries contained hyphenated terms such as `top-secret` or `in-memory`. Terms are now cleanly split and joined with `ACCUM`.

#### 🚀 Concurrency Safety & Parallel Dispatch
- Added `oracle_search` to `_PARALLEL_SAFE_TOOLS` in Hermes Agent (`tool_dispatch_helpers.py`), unlocking safe concurrent execution alongside parallel tool dispatches.
