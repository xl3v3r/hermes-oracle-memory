# Release Notes — v1.1.1.3

## Version 1.1.1.3 (2026-11-14) — Reliability fixes, handoff rows, bounded timeouts, safe test suite

### Summary
`hermes-oracle-memory` v1.1.1.3 is a reliability and capability release. It fixes the error-classification bugs that locked every embedding read/write out for 60 s after unrelated failures, keeps the global `on_skill_lifecycle` hook working after the provider it was bound to shuts down, bounds the connection-pool wait that could hang a tool call for minutes, adds **handoff rows** (caller-chosen `memory_id` + `tags`, and a new `oracle_get` exact-read tool), and makes the test suite safe to run on a host with a live install.

### Why
1. **ONNX cooldown tripped by non-model errors.** Any embedding-path exception — a broken pooled session (`ORA-01002 fetch out of sequence`, lost connection), a client call timeout (`DPY-4024`), a plain Python error (`'NoneType' object is not iterable`), or a data error such as a duplicate `memory_id` (`ORA-00001`) — was treated as an in-database model failure. That put the process into the 60 s ONNX cooldown, so every `oracle_search`/`oracle_add` failed with "embedding model is unavailable" although the model was fine.
2. **Stale read clamp on write connections.** python-oracledb runs `session_callback` only for new connections, so a pooled connection whose `call_timeout` was tightened by a read path kept that clamp when reused for a write → `DPY-4024` on inserts (which then also tripped the cooldown above).
3. **Hook bound to a dead provider.** Hermes binds the plugin's global `on_skill_lifecycle` hook to the newest provider instance. Once that instance's agent shut down, every skill-lifecycle event in every other session failed with `pool not initialized`. Separately, closing the pool while the background writer still held a connection raised `list.remove(x): x not in list` and failed every queued row.
4. **Unbounded pool acquire.** The pool used `POOL_GETMODE_WAIT`, which ignores `wait_timeout`; when all sessions were busy, `acquire()` blocked forever (observed: `oracle_search` calls of 171 s and 403 s that only ended when the user stopped the turn).
5. **Search timeout knob not wired.** The documented `HERMES_ORACLE_SEARCH_TIMEOUT_MS` only fed the pool fingerprint; the per-query clamp read an undocumented seconds variable and was fixed at 13 s — below the observed recall p99 (~14 s). `HERMES_ORACLE_INGEST_TIMEOUT=600.0` (a float) crashed `int()` in `initialize()` and `scripts/sync_skills.py`.
6. **No way to hand work between agents.** Facts only had random ids and no tags, so one agent could not leave state under a known key for another agent or session to pick up.
7. **Destructive test suite.** `test_zero_drift_with_installed_plugin` copied the checkout over `~/.hermes/plugins/oracle` and every `~/.hermes/profiles/*/plugins/oracle` (deleting extras and creating symlinks), and `test_skill_bundles.py` wrote symlinks into `~/.hermes/skills`. Running `pytest` from any clone or PR branch silently replaced the live plugin.

### What changed
- **Error classification** (`__init__.py`): new `_is_session_error()` (session/connection codes `ORA-00028/01001/01002/03113/03114/03135`, `DPY-4005/4011/4024`, `DPI-*` connection errors) and `_is_onnx_model_failure()` (only a server-side `ORA-` error that is neither a session error nor a data/constraint error — `ORA-00001/00942/01400/01438/02290/02291/02292/12899/40441` — can start the cooldown). Python exceptions never do.
- **Connection hygiene**: `_CallTimeoutPool` re-applies the default `call_timeout` on every acquire; `_acquire_checked()` drops (instead of returning) a connection that raised a session-level or non-Oracle error; `_search()` retries once on a broken-session error with a fresh connection (timeouts are not retried — they already spent the caller's budget).
- **Hook survival**: a per-process registry of live providers; `on_skill_lifecycle` and the background writer route to a live provider when their own pool is gone, and drop (logged once) instead of queueing into a dead pool. `shutdown()` no longer closes the pool under an active writer.
- **Search results** (from PR #3): `created_at` is projected by the search SQL itself, removing the secondary per-search query; results are JSON-safe (datetimes serialized as ISO-8601).
- **Handoff rows**:
  - `oracle_add(memory_id=…, tags=[…])` — optional caller-chosen stable id (1–128 chars: letters, digits, `_ . : @ / -`; single fact only) with **upsert** semantics (UPDATE in place, else INSERT; a concurrent-insert race falls back to UPDATE). Tags are lower-cased, de-duplicated, max 32 × 64 chars, stored in `metadata.tags`. An upsert without `tags` keeps the existing tags.
  - `oracle_update(memory_id, content?, tags?)` — `content` is now optional; a tags-only update does not re-embed and works during an ONNX cooldown.
  - `oracle_search(…, tags=[…], tag_match="all"|"any")` — tag filter in text, vector, and hybrid modes.
  - **New tool `oracle_get`** — exact read by `memory_id`/`memory_ids` and/or `tags` (+ optional session/agent/target filters, `limit` ≤ 100), newest first, reports `missing` ids. No embedding involved, so it works while ONNX is cooling down.
  - Tag predicates are fully bound (`JSON_EXISTS … PASSING`), never interpolated, and are evaluated on `JSON_QUERY(metadata, '$.tags')` so the asynchronously-synced JSON search index on `metadata` is bypassed — a handoff row is readable immediately after it is written.
  - System-prompt rule 10 (HANDOFFS) documents the convention `memory_id="handoff:<project>:<topic>"`, `tags=["handoff","project:<name>"]`.
- **Timeouts**:

  | Setting | Env variable | 1.1.1.2 | 1.1.1.3 |
  |---|---|---|---|
  | Recall query clamp | `HERMES_ORACLE_SEARCH_TIMEOUT_MS` | 13 s (env ignored) | **30 s**, env honored (legacy `HERMES_ORACLE_SEARCH_TIMEOUT` seconds still read when `_MS` is unset) |
  | Pool acquire wait | `HERMES_ORACLE_POOL_WAIT_TIMEOUT_MS` | unbounded (`POOL_GETMODE_WAIT`) | **20 s** (`POOL_GETMODE_TIMEDWAIT`, raises `DPY-4005`) |
  | Idle session close | `HERMES_ORACLE_POOL_IDLE_TIMEOUT` | 5 s | **300 s** (fewer TLS handshakes to ADB) |
  | Prefetch wait per turn | `HERMES_ORACLE_PREFETCH_WAIT` | 0.5 s | **3 s** (clamped 0.1–7 s, under the host's 8 s budget) |
  | Ingest / write budget | `HERMES_ORACLE_INGEST_TIMEOUT` | 600 s, integer only | 600 s, int or float |

  Bad or non-positive values fall back to the default (logged once). The pool fingerprint now includes the wait and idle settings.
- **Tests and CI**: the drift test is read-only and opt-in (`HERMES_ORACLE_DRIFT_DIR=<installed plugin dir>`); `tests/test_skill_bundles.py` is removed; the version test no longer deletes files; new `tests/test_handoff.py` (25 tests) and timeout tests. New `.github/workflows/tests.yml` runs the suite on Python 3.11 and 3.13 with a sandboxed `HOME` plus a version-drift check. CLA workflow allowlist syntax fixed (comma-separated; bot accounts no longer block PRs).
- Version bump `1.1.1.2` → `1.1.1.3` across `VERSION`, `plugin.yaml`, `pyproject.toml`, `__version__`, README/INSTALL badges, and both `skills/*/SKILL.md` pins. Release-notes rotation: `RELEASE_NOTES_1.1.1.2.md` → `RELEASE_NOTES_1.1.1.3.md` (v1.1.1.2 details remain in `CHANGELOG.md`).

### Upgrade notes
- **No schema change.** Tags live in the existing JSON `metadata` column; existing rows simply have no tags.
- **Check your `.env`:** if it pins `HERMES_ORACLE_SEARCH_TIMEOUT_MS=13000` (as earlier docs suggested), that value now actually takes effect. Remove the line, or set `30000`, to use the new default.
- The pool fingerprint changes, so long-lived processes rebuild their pool once after the upgrade.
- **Never run the test suite of v1.1.1.2 or older on a host with an installed plugin** — see "Destructive test suite" above. From 1.1.1.3, running it with `HOME` pointed at a scratch directory is still recommended.

### Verification
- Test suite: 278 passed, 3 skipped (sandboxed `HOME`; installed plugin checksum unchanged before/after).
- Live end-to-end against Oracle ADB 26ai: handoff insert, upsert, tag-preserving upsert, `oracle_get` by id/tags (`all`/`any`), tags-only update, tag-filtered search in text/vector/hybrid modes, and cleanup — 15/15 checks pass under the new pool settings.
