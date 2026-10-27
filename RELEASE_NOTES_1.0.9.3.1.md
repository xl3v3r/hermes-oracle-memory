# Release Notes — Hermes Agent Oracle 26ai Memory Provider v1.0.9.3.1

> [!IMPORTANT]
> **CRITICALITY NOTICE**: v1.0.9.3.1 updates are CRITICAL to functionality. Introduces the native Skill Library Indexing Engine (`oracle_sync_skills` and `scripts/sync_skills.py`), provides byte-safe UTF-8 boundary clamping (`_safe_varchar_bytes`) across all insertion and update paths to prevent `ORA-12899`, establishes a root `VERSION` file, and enforces zero drift across repository, installed plugin, and all Hermes Agent profile environments.

### What's New in v1.0.9.3.1

#### 🧠 Native Skill Library Indexing (`oracle_sync_skills` & `scripts/sync_skills.py`)
- **Native Skill Discovery**: Automatically locates and parses all `SKILL.md` documents across `~/.agents/skills` and `~/.hermes/skills` (discovering 222+ skills).
- **Robust Frontmatter & Body Parsing**: Handles YAML frontmatter with folded/literal chomping (`>-`), bullet lists, and nested mappings, extracting descriptions, prompt triggers, action patterns, and parameter summaries.
- **Durable Semantic Memory Ingestion**: Ingests parsed skill definitions directly into Oracle AI Database 26ai persistent memory tagged with `agent:skills`, `category:skill`, and `skill:<name>`.
- **Execution & Optimization Hints**: Uses `/*+ NO_PARALLEL */` hints and sequence-length safe embedding text clamping (`1200` chars) to ensure maximum vector database throughput and prevent `ORA-54401`.
- **Dual Invocation Modes**: Callable both via the Hermes tool interface (`oracle_sync_skills`) and as a standalone CLI tool (`python3 scripts/sync_skills.py`).

#### 🛡️ Byte-Safe UTF-8 Clamping (`_safe_varchar_bytes`)
- **UTF-8 Byte Boundary Clamping**: Clamps `content_txt` to strictly 4000 UTF-8 bytes without slicing through multibyte code points.
- **Universal Fix for ORA-12899**: Applied across `_insert`, `_insert_many` (both in-database ONNX and precomputed loops), and `oracle_update` to guarantee that text with wide characters, emojis, or formatting never exceeds Oracle `VARCHAR2(4000 CHAR)` limits.

#### 📦 Version Tracking & File Structure Consistency
- **Dedicated Root VERSION File**: Establishes a standardized root `VERSION` file (`1.0.9.3.1`) for explicit version tracking and package verification.
- **Operational & Test Script Consolidation**: Ensures all test and operational scripts (`audit_embeddings.py`, `backfill_embeddings.py`, `import_conversations_ordered.py`, `reembed_embeddings.py`, `run_tests.py`, `setup_onnx_model.py`, `sync_skills.py`, `verify_provider.py`) reside in the `scripts/` directory.

#### 🔒 Zero Drift & Core File Integrity
- **Core Repository Isolation**: Strictly zero modifications made to Hermes Agent core repository files (`~/.hermes/hermes-agent`), keeping upstream mainstream branches cleanly intact.
- **100% Repository, Plugin, & Profile Parity**: Synchronized byte-for-byte across `/home/rocky/hermes-oracle-memory`, `~/.hermes/plugins/oracle`, and all Hermes Agent profiles (`backend`, `ui`, `safeguard`, `watcher`, `release`, `orchestrator`, `recon`).
