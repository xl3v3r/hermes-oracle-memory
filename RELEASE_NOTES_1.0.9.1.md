# Release Notes — Hermes Agent Oracle 26ai Memory Provider v1.0.9.1

> [!IMPORTANT]
> **CRITICALITY NOTICE**: v1.0.9.1 updates are CRITICAL to functionality. Neutralizes Oracle Text reserved keyword query syntax collisions by wrapping tokens in curly braces `{}`, decouples text index errors (`DRG-`, `ORA-29902`, `ORA-30600`) from ONNX model health to prevent spurious cooldown trips, and implements automatic fallback to pure vector search when text index errors occur.

### What's New in v1.0.9.1

#### 🛡️ Oracle Text Reserved Keyword Literal Escaping (`{}`)
- **Neutralize Reserved Words in `_contains_query()`**: Escapes every query token inside curly braces `{token}` (e.g. `{not}`, `{and}`, `{accum}`, `{minus}`, `{within}`, `{near}`) so Oracle Text evaluates all terms strictly as literal word matches rather than query operators.
- **Empty Query Neutralization**: When a query cleans down to no valid search tokens (e.g., empty string, punctuation only, or short words < 3 characters), returns `"{memory}"` safely escaping the default fallback keyword.
- **Eliminates `DRG-` & `ORA-29902` Query Parse Errors**: Prevents queries containing conversational terms matching Oracle Text operator keywords (`AND`, `OR`, `NOT`, `ACCUM`, `MINUS`, `WITHIN`, etc.) from failing with syntax errors during text index searches.

#### ⚡ Decoupled Oracle Text Errors from ONNX Health Monitoring
- **Isolated Failure Domains**: In `_execute_hybrid_search()`, exceptions are inspected for Oracle Text error codes (`DRG-`, `ORA-29902`, `ORA-30600`).
- **Prevents False ONNX Cooldowns**: Only actual vector/model errors call `self._onnx_mark_failed()`. Syntax or indexing errors originating from the Oracle Text layer no longer penalize the ONNX embedding model or trigger ONNX cooldown timers.

#### 🔄 Graceful Hybrid-to-Vector Search Fallback
- **Resilient Prefetch & Turn Search**: When hybrid search encounters an Oracle Text error during index lookup, the provider automatically falls back to executing pure vector search (`_build_vector_search_sql`).
- **Zero Interrupted Turns**: Memory retrieval and prefetch continue uninterrupted using dense vector similarity even if text indexing encounters an unescaped or degraded index condition, ensuring Hermes Agent turns never abort due to text search index issues.

#### 🔒 Strict In-House Data Boundary
- All embeddings are computed 100% in-house using Oracle Autonomous AI Database 26ai native in-database ONNX inference (`VECTOR_EMBEDDING` with `BGE_BASE_EN_V1_5`).
- Zero external third-party API dependencies or data transit.
