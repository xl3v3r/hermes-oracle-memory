# Release Notes — Hermes Agent Oracle 26ai Memory Provider v1.0.9

> [!IMPORTANT]
> **CRITICALITY NOTICE**: v1.0.9 updates are CRITICAL to functionality. Implements Option B automated `on_skill_lifecycle` observer hook recording skill invocations as vectorized facts directly into Oracle 26ai ADB persistent memory with zero external API transit.

### What's New in v1.0.9

#### 🧠 Automated Skill Lifecycle Memory Hook (Option B)
- **Automatic Lifecycle Event Observation**: In `register(ctx)` in `__init__.py`, the memory provider registers an observer hook on `on_skill_lifecycle` via `ctx.register_hook("on_skill_lifecycle", callback)`.
- **Vectorized Fact Persistence**: Intercepts `used` and `loaded` skill actions (`action in ("used", "loaded")`) to record skill activations directly as durable facts in Oracle 26ai ADB memory.
- **Rule 8 Bracketed Metadata Tags & Rule 2 Compact Form**: Formats compact facts (<200 characters to comply with Rule 2 and prevent 280-character prefetch truncation) stamped with structured bracketed tags:
  ```text
  [skill_usage] [skill:<skill_name>] [action:<action>] [ts:<ISO8601>] [session:<session_id>] Skill '<skill_name>' activated in session '<session_id>'.
  ```
- **Asynchronous, Non-Blocking & Fail-Safe Ingestion**: Skill usage events are queued through `provider._queue_background_insert(...)`, leveraging the single background worker thread. Any unexpected exception during hook dispatch is caught and debug-logged, guaranteeing that agent turn execution is never delayed or broken by persistence operations.
- **Strict 100% In-House Data Boundary**: All facts are embedded exclusively in-database using Oracle Autonomous AI Database 26ai native ONNX embeddings (`VECTOR_EMBEDDING`). Zero external API transit is maintained across all execution pathways.
