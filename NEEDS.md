# hermes-oracle-memory — what it needs now

**Version in play:** 1.1.0.1 (`VERSION`, `pyproject.toml`, `plugin.yaml`, `__init__.py` agree)
**Installed at:** `~/.hermes/plugins/oracle` (pinned checkout, tag `v1.1.0.1`)
**Environment:** Oracle 26ai ADB, 2 CPUs, in-DB ONNX `BGE_BASE_EN_V1_5`, `HERMES_ORACLE_DISABLE_EXTERNAL_EMBED=1`, table `hermes_agent_memory`
**Updated:** 2026-11-07, v1.1.0.1 fleet-sync hardening release

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
