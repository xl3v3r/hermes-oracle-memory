# Release Notes — v1.1.1.5

## Version 1.1.1.5 (2026-11-15) — Skill-sync and backfill follow-ups

### Summary
`hermes-oracle-memory` v1.1.1.5 fixes the follow-ups from the 1.1.1.4 review tracked in #14, except the profile isolation item, which stays open. A sync over part of a profile's skill dirs no longer treats the profile's own copies as foreign, and a copy that really is foreign can now be replaced on request. The lifecycle hook no longer treats a link into `.archive/` as a live skill. The embedding backfill no longer retries during shutdown or drops rows when the driver cannot report row counts. The test suite now refuses at run time to create any link outside the temp dir.

### Why
1. **`skipped_foreign` could not be cleared.** The bulk sync treated any copy of this host stored from a SKILL.md outside the dirs being scanned as another profile's. A sync over an explicit `--dirs` subset therefore skipped the copies the profile had stored from its other dirs. A copy left from a dir that had been removed from the configuration was skipped too. Neither was ever updated (not even with `force`) or pruned.
2. **The lifecycle lookup could find archived skills.** `_find_skill_file` skipped hidden directories by name only. A non-hidden link that resolved into `.archive/` counted as live, so an `archived` event kept the rows and an ingest event indexed a skill that the bulk sync ignores. The lookup also stopped six levels below a root, while the bulk sync walks any depth.
3. **Backfill edge cases.** During shutdown, a row whose stored text kept changing was re-read, re-embedded and re-queued until its attempts ran out, all inside the drain budget. When `getarraydmlrowcounts()` failed, rows the UPDATE had skipped were dropped without a log line; only the startup scan picked them up.
4. **The test link guard checked only the source text.** A comment could satisfy the `_link_in_tmp` check, and `os.link`, `hardlink_to`, `from os import symlink` and shell `ln -s` were not caught.

### What changed
- **Foreign copies**:
  - The bulk sync judges ownership against the profile's own skill dirs: its skills dir and `~/.agents/skills`, plus any `dirs` passed. A sync over a subset now updates the copies stored from the other dirs.
  - A copy is still foreign when its stored SKILL.md exists outside all of them. That covers another profile with the same host id, or a dir no longer configured. It is kept and counted in `skipped_foreign` / `foreign_skills`, and the result carries a `hint`.
  - A `--dirs` run over a dir outside the defaults does not replace a copy stored from a live SKILL.md in a default dir. Otherwise later default syncs and the lifecycle hook would find this run's copy outside the profile's dirs and keep skipping it as foreign.
  - The new `replace_foreign` flag replaces such a copy after a warning. It is available as `oracle_sync_skills(replace_foreign=true)`, `sync_skills.py --replace-foreign` and `sync_skills_library(replace_foreign=True)`. `force` does not imply it.
- **Lifecycle lookup**:
  - A SKILL.md counts as live only when it exists and its resolved path has no part in the ignored set (`.archive`, `.git`, `.venv`, `.pytest_cache`, `.locks`, `.curator_backups`), the same rule `discover_skills` applies. This governs both which file the hook ingests and which stored copies `archived` keeps.
  - The lookup and discovery share one walker. It skips hidden dirs, follows links and enters each real directory once, so it works at any depth and ends on link loops.
- **Backfill**:
  - Once shutdown has begun, rows not confirmed written are logged (with a count) instead of re-read and re-queued. The startup scan re-queues any row still without an embedding.
  - When the driver cannot report per-row counts, every row of the group is re-checked: rows that got their vector are done, the rest are re-queued with the text the database holds.
- **Test suite**:
  - `tests/conftest.py` wraps `os.symlink` and `os.link` for the whole run. `Path.symlink_to` and `Path.hardlink_to` call these, so they are covered too; on Python 3.10, where `pathlib` keeps its own references, its accessor is patched as well.
  - The wrappers refuse any link whose directory or target resolves outside `tempfile.gettempdir()`, as well as links relative to a `dir_fd`. They refuse every link if the temp dir contains the user's real home, where the installed plugin lives.
  - The source scan in `test_suite_never_writes_outside_checkout` parses the `_link_in_tmp` helper, so a comment or docstring no longer satisfies it. It also flags aliased calls (`o.symlink`, `o.link`, `from os import symlink`) and shell links (`ln -s`, `-fs`, `--symbolic`, `/bin/ln`).
- Version bump `1.1.1.4` → `1.1.1.5` across `VERSION`, `plugin.yaml`, `pyproject.toml`, `__version__`, README/INSTALL badges, both `skills/*/SKILL.md` pins, `AGENTS.md` and `NEEDS.md`. Release notes rotated: `RELEASE_NOTES_1.1.1.4.md` → `RELEASE_NOTES_1.1.1.5.md`; the v1.1.1.4 details remain in `CHANGELOG.md`.

### Upgrade notes
- **No schema change and no migration.** Install over 1.1.1.4 and restart the gateway.
- A sync that reported `skipped_foreign` under 1.1.1.4 because of a `--dirs` subset now updates those copies. Copies from removed dirs or from another profile still need `replace_foreign`. Check `foreign_skills` first, because replacing a copy that another profile still uses gives that profile's skill this profile's text.

### Known limitations
- **Profiles that share a host id are not isolated (#14, open).** Two cases remain:
  - A skill dir that one profile links into another can be rewritten by the second profile, then archived or pruned by it.
  - A pre-1.1.1.4 row that stores a link target can be deleted when one profile archives its link while another profile still uses the target.

  The copy returns on that profile's next sync or edit. Set `HERMES_HOST_ID` per profile to keep profiles apart.

### Verification
- Test suite: 479 passed, 4 skipped (sandboxed `HOME`), identical with `HERMES_ORACLE_SEARCH_TIMEOUT_MS=13000`. The extra skip is the Python 3.10 accessor check, which runs only on 3.10.
- Of the 29 new tests that run here, 25 fail against 1.1.1.4. The other 4 cover behaviour that was already right and must stay so: termination on link loops, links inside the temp dir being allowed, and a `--dirs` run replacing a default copy whose file is gone.
- The accessor patch was exercised on a real `pathlib._NormalAccessor` (Python 3.9, same layout as 3.10): `symlink_to` outside the temp dir is refused.
- `mypy` (3.11) clean on `__init__.py`; `ruff` (F, E9) clean on `__init__.py` and `scripts/`.
- Live, read-only against Oracle ADB 26ai on two hosts (48 and 78 skills):
  - The hook's lookup finds every skill that discovery finds.
  - The new liveness check agrees with a plain file check for every stored copy of the host.
  - Sync dry runs (meta, full, and a one-dir subset) give the same counts as 1.1.1.4, with `skipped_foreign` 0.
