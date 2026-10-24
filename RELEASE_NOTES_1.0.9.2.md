# Release Notes — Hermes Agent Oracle 26ai Memory Provider v1.0.9.2

> [!IMPORTANT]
> **CRITICALITY NOTICE**: v1.0.9.2 updates are CRITICAL to functionality. Clamps embedding text to 1200 characters to prevent ORA-54401 ONNX sequence length violations, decouples ORA-54401/ORA-51815 shape errors from ONNX cooldown, lowers ONNX retry cooldown from 300s to 60s, implements fallback to text search during ONNX cooldown, and prevents silent turn loss on insert errors.

### What's New in v1.0.9.2

#### 📏 Safe Embedding Input Clamping (`_safe_embed_text`)
- **Prevent Sequence Length Overflow**: Clamps input text destined for in-database `VECTOR_EMBEDDING` to 1200 characters (`_safe_embed_text()`).
- **Eliminates `ORA-54401` Broadcast Errors**: In dense text environments (code snippets, UUIDs, stack traces, URLs, nested JSON), raw inputs exceeding ~512 tokens caused in-database ONNX runtime dimension mismatch errors (`ORA-54401: axis == 1 || axis == largest was false ... 512 by 755`). Clamping to 1200 characters guarantees token counts remain strictly within the 512 sequence limit.
- **Preserves Full Text Fidelity**: Full conversation turns remain stored in the CLOB `content` column without truncation, and up to 4000 characters are preserved in `content_txt` for Oracle Text lexical search.

#### 🛡️ Decoupled Shape & Token Length Errors from ONNX Health Monitoring
- **Isolated Tensor Shape Errors**: In `_insert()`, `_insert_many()`, `oracle_update()`, `_embed_many_tagged()`, `_execute_hybrid_search()`, and `_search(mode="vector")`, exceptions are checked for tensor dimension and input sequence errors (`ORA-54401`, `ORA-51815`).
- **Prevents Cooldown Death Spirals**: Shape errors originating from oversized inputs are user-content errors rather than database infrastructure failures. They no longer call `self._onnx_mark_failed()`, eliminating the perpetual cooldown loop where long prompts repeatedly locked the ONNX embedding engine.
- **Manifest Validation & Hook Declaration**: Formally declared `provides_hooks: [on_skill_lifecycle]` in `plugin.yaml` ensuring full pass in Hermes plugin admission validation (`hermes plugins validate`).

#### ⏱️ Reduced ONNX Cooldown Duration (60s)
- **Fast Self-Healing**: Lowered `_ONNX_RETRY_AFTER_SECS` from 300.0s (5 minutes) to 60.0s (1 minute).
- **Rapid Recovery**: Genuine transient database maintenance, pool reconnections, or ONNX model reloads now recover five times faster without requiring manual gateway intervention.

#### 🔄 Graceful Hybrid-to-Text Fallback During Cooldown
- **Resilient Memory Recall**: When `_execute_hybrid_search()` detects that the ONNX model is in cooldown, it automatically executes Oracle Text search (`_build_text_search_sql`) instead of failing or throwing `RuntimeError`.
- **Zero Interrupted Searches**: Users and agents continue receiving relevant lexical memory search results even while vector embeddings are recovering.

#### 💾 Defense-in-Depth Against Silent Turn Loss
- **Null-Embedding Fallback on Insert**: If an insert fails due to an ONNX shape error, the provider catches the error and executes a defensive fallback insert with `NULL` embedding.
- **Guaranteed Fact Retention**: Conversation turns are never discarded or silently lost; turns are persisted and immediately discoverable via Oracle Text `CONTAINS` queries.

#### 🔒 Strict In-House Data Boundary
- All embeddings are computed 100% in-house using Oracle Autonomous AI Database 26ai native in-database ONNX inference (`VECTOR_EMBEDDING` with `BGE_BASE_EN_V1_5`).
- Zero external third-party API dependencies or data transit.
