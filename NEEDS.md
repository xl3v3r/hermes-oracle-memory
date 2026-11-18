# hermes-oracle-memory — what it needs now

**Version in play:** 1.1.2 (`VERSION`, `pyproject.toml`, `plugin.yaml`, `__init__.py` agree)
**Installed at:** `~/.hermes/plugins/oracle` (pinned checkout, tag `v1.1.2`)
**Environment:** Oracle 26ai ADB, 2 CPUs, in-DB ONNX `BGE_BASE_EN_V1_5`, `HERMES_ORACLE_DISABLE_EXTERNAL_EMBED=1`, table `hermes_agent_memory`
**Updated:** 2026-11-18, v1.1.2 review fixes (#14, #16-#22, #24-#27, #32)

---

## Resolved

| # | Item | Resolved in |
|---|---|---|
| 1 | Read path had no `ORA-51815` retry — raised instead of waiting/degrading | 1.0.9.5 (3-attempt backoff + text degradation) |
| 2 | No vector-index maintenance path / DDL not recoverable | 1.0.9.5 (`VECTOR_INDEX_DDL` + `scripts/rebuild_index.py`) |
| 3 | Full-mode sync burned ~9 min on a serial DELETE pre-pass | 1.0.9.5 (batched `executemany`, chunks of 500, scoped to target set) |
| 4 | Batch size fixed at 20 and not exposed | 1.0.9.5 (`--batch-size`, default 50) |
| 5 | `references/` never indexed | 1.0.9.5 (reference chunking, sequential indices, composite SHA-256) |
| 6 | Local skills under-indexed | 1.0.9.5 (full local sync verified; `pro-se-litigation` = 40 chunks) |
| — | `_JUNK` filter hid every `[skill_ingest]` row | 1.0.9.4.2 |
| — | `oracle_sync_skills` tool call crashed on `@dataclass` load | 1.0.9.4.2 |
| — | **Skill rows written but unreachable by every search mode** | **1.0.9.6** |
| #5 | Skill rows of all hosts shared one id; one host's sync or archive deleted another host's copy | 1.1.1.4 (`skill:<host>:<name>:c<N>`, own-row deletes, opt-in prune, migration with reverse step) |
| #6 | Lifecycle hook did file and DB work on the caller's thread, dropped events during a write, ignored the profile | 1.1.1.4 (one writer per skill, coalesced re-run, profile-aware roots) |
| #7 | Writes during an ONNX cooldown were lost; backfill retried without backoff | 1.1.1.4 (NULL-embedding write + backfill with backoff, startup re-queue) |
| #8 | Old pool leaked on re-init; shutdown could close the pool under a busy worker | 1.1.1.4 |
| #9 | Transactional insert retried inside a broken transaction; `ORA-00060` tripped the cooldown | 1.1.1.4 |
| #10 | Recall path: reused cursor on retry, LOB round trips, fetch sizing, ranking after the cut | 1.1.1.4 |
| #11 | Tool surface: unbounded batches and output, schema gaps, memory mirror ignored replace/remove | 1.1.1.4 |
| #12 | Scripts read the env file unconditionally; non-SPDX license; wildcard CLA bot allowlist | 1.1.1.4 |
| #14 | No override for `skipped_foreign`; lifecycle lookup treated links into `.archive/` as live; backfill edge cases (shutdown re-queue, no row counts); source-only test link guard | 1.1.1.5 (profile-root ownership + `--replace-foreign`, liveness by resolved path, loop-safe walk, runtime link guard). Open: profiles sharing a host id |

## 1. Skill retrieval starvation — RESOLVED in 1.0.9.6

Role priority (`ORDER BY CASE WHEN role = 'fact' THEN 1 ELSE 2 END`) sat *ahead of* the
similarity key inside each branch's `FETCH FIRST :k`, so the candidate window
(`k = fetch_k = 2 * top_k = 10`) was always filled by `role='fact'` rows. With ≈1.4k facts
and ≈31.7k skill chunks, **no skill row could ever be returned** in hybrid, vector or text
mode, no matter how close the match.

Fixed by removing the role term from the three SQL builders
(`_build_text_search_sql`, `_build_vector_search_sql`, `_build_hybrid_search_sql`) so
candidates are selected by similarity alone, and applying role preference afterwards in
`_WEIGHT_ROLE` (`skill: 0.9` vs `fact: 1.0`). Guarded by two unit tests and a live
reachability check in `tests/verify_provider.py`.

Rule for future changes: **never rank by role inside the top-k cut.** Role preference
belongs in `_WEIGHT_ROLE`, which runs after candidate selection.

## 2. Corpus and bundle wiring is per-host — OPEN (deployment, not code)

On this fleet some hosts have no `~/.hermes/skill-bundles/` nor
`~/.hermes/archive/casemark-skills`, so:

- the 17 curated manifests are absent (nothing to resolve, nothing to symlink);
- the CaseMark corpus is not present at all, so those 2,083 skills are not indexed here;
- `tests/test_skill_bundles.py` skips by design on such hosts (it fails only where
  bundles were partially deployed — a real drift signal worth keeping).

Locally discovered skills under `~/.hermes/skills` **are** indexed and retrievable.

### Decision required (per host)

Either symlink `~/.hermes/skills/casemark` → the archive and deploy the manifests, or
accept that the corpus is retrieved only through `oracle_search` and skip the bundles.
Retrieval works either way; only the slash-command bundles need the symlinks.

## 3. Verification state (seeding host, 2026-11-01)

- Table: 33,171 rows — 31,734 `role='skill'` (2,301 distinct skills, all embedded), 1,379 `role='fact'`, 33 `turn`, 25 `session`.
- Indexes: `HERMES_AGENT_MEMORY_VEC_IDX` (VECTOR, VALID), `HERMES_AGENT_MEMORY_TXT_IDX` (DOMAIN on `CONTENT`, VALID), `HERMES_AGENT_MEMORY_JSON_IDX` (DOMAIN on `METADATA`, VALID).
- Skill recall post-fix: `legal-memo` for legal drafting, `systematic-debugging` for debugging, `document-to-action-items` for contract extraction; factual queries still return facts only.
- Test suite: green (see `tests/run_tests.py`).
