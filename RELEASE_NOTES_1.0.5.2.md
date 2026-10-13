### What's New in v1.0.5.2

#### 💰 Token & API Cost Reduction
- **Zero External Embedding Costs via In-Database ONNX Inference**:
  - Leverages in-database ONNX inference (`VECTOR_EMBEDDING` in Oracle 26ai with `BGE_BASE_EN_V1_5`), completely eliminating paid third-party embedding API calls and recurring vector token costs.
- **In-Memory Embedding Deduplication**:
  - Implemented in-memory deduplication in `_embed_many` batch requests, ensuring identical text fragments across turns and contexts are processed once, preventing redundant embedding API token spend.
- **Accurate Hybrid Retrieval (Oracle Text + Vector)**:
  - Fuses dense vector search with exact lexical Oracle Text scoring to return high-precision context, avoiding dumping excessive irrelevant context into LLM prompt windows and saving prompt tokens.

#### ☁️ Native Oracle Cloud (OCI) Architecture Integration
- **Direct OCI Autonomous AI Database 26ai Vector Capabilities**:
  - Direct integration with Oracle ADB 26ai native vector functions, `VECTOR(768, FLOAT32)` indexing, and in-database chunking via `DBMS_VECTOR.UTL_TO_CHUNKS`.
- **Native OCI GenAI Service Fallback**:
  - Seamless fallback to OCI Generative AI inference (`cohere.embed-v4.0`) utilizing native OCI Instance Principals and API signing for secure, keyless execution on OCI compute.
- **Connection & Signer Caching Across Agent Sessions**:
  - Caches and reuses OCI GenAI inference clients and Instance Principals security signers across agent sessions, eliminating client reconstruction overhead and token renewal delays.

#### ⚡ Performance & Reliability Optimizations
- **Zero-Latency In-Database ONNX Hot Paths**:
  - Eliminated redundant client-side embedding generation during vector and hybrid search when Oracle ADB in-database ONNX inference (`VECTOR_EMBEDDING`) is enabled.
  - Avoided client-side embedding computation on `oracle_update` calls prior to database execution.
- **Fail-Fast ONNX Cooldown & Isolated Fallback**:
  - Added a 5-minute (300s) cooldown window upon in-database ONNX inference errors across embed, insert, and update operations to prevent repeatedly querying a broken model.
  - Centralized remote embedding fallback (`_embed_external`) ensuring failed ONNX operations fall back directly to remote providers (OCI GenAI / OpenRouter) without recursive attempts.
- **Asynchronous Non-Blocking Lifecycle Persistence**:
  - Converted turn persistence (`sync_turn`) and session closure (`on_session_end`) to non-blocking background queueing (`_queue_background_insert`), eliminating the previous 2-second per-turn join penalty while guaranteeing orderly thread cleanup on shutdown.
- **Database Fetch & Query Performance Optimization**:
  - Tuned Oracle cursor buffer sizing (`arraysize` and `prefetchrows`) up to fetch limits (`max(64, k)`), reducing ADB round-trips for result sets.
  - Compiled Oracle Text query sanitation regular expression (`_CONTAINS_CLEAN_RE`) at module load time.
  - Added fast-path short-circuit in hybrid search to bypass reranking when candidate count is ≤ 1.
- **Dedicated Installation & Setup Guide**:
  - Added separate [INSTALL.md](INSTALL.md) covering full installation, OCI environment setup, in-database ONNX configuration, and legacy migration.
