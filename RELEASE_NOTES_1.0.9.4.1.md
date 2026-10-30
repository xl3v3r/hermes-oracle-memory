# Release Notes — Hermes Agent Oracle 26ai Memory Provider v1.0.9.4.1

> [!IMPORTANT]
> **CRITICALITY NOTICE**: v1.0.9.4.1 introduces dynamic retrieval weighting with exponential recency decay, candidate telemetry filtration (`[skill%`), role weighting, short content length penalties, and test harness self-execution guards against `SameFileError`. 100% zero drift is maintained across repository, installed plugin, and all Hermes Agent profile environments.

### What's New in v1.0.9.4.1

#### ⚖️ Dynamic Retrieval Weighting (`_apply_weights`)
- **Exponential Recency Decay**: Fuses memory age into final relevance scores using `0.85 + 0.15 * exp(-age_days / 120)`. Ensures fresh facts take precedence over superseded historical entries while preserving a 0.85 floor for durable foundational knowledge.
- **Role Prioritization Multipliers**: Evaluates candidate role types (`fact: 1.0`, `user: 0.9`, `session: 0.9`, `turn: 0.75`, `assistant: 0.75`).
- **Telemetry Dampening & Noise Filtering**: Penalizes lifecycle logging (`[skill_usage]`, `[skill_vectorization*]`) with a 0.05 multiplier and eliminates telemetry records from search candidate pools via `_JUNK` filter (`AND NVL(content_txt, 'x') NOT LIKE '[skill%'`).
- **Short Content Adjustment**: Applies a 0.9 factor to low-information content under 25 characters.
- **Resilient Batch PK Lookup**: Resolves `created_at` timestamps for candidate items in chunked queries of 500 IDs, falling back cleanly to unweighted scores on any database exception.

#### 🛡️ Test Harness Hardening
- **Zero Drift Protection in In-Place Runs**: `tests/test_oracle.py` now guards against `repo_dir.resolve() == plugin_dir.resolve()`, preventing `shutil.SameFileError` when running directly inside `~/.hermes/plugins/oracle`.

#### 🧪 Expanded Test Coverage
- **Retrieval Weighting Unit Tests**: Validates recency decay curves, role multipliers, telemetry penalties, short content adjustments, batch timestamp lookups, and exception fallback behaviors.

#### 🔄 Complete Zero-Drift Synchronization
- **Zero Drift Enforcement**: 100% byte-for-byte synchronization across the canonical repository, `~/.hermes/plugins/oracle`, and all bot profile environments (`edgebot`, `qoam-recon`, `qoam-backend`, `qoam-release`, `qoam-safeguard`, `qoam-ui`, `qoam-watcher`, `qoam-orchestrator`).
- **Core File Isolation**: Zero modifications made to Hermes Agent core repository files.
