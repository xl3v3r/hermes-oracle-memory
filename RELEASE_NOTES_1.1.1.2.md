# Release Notes — v1.1.1.2

## Version 1.1.1.2 (2026-11-10) — Search-timeout default correction, single timeout read, docs accuracy

### Summary
`hermes-oracle-memory` v1.1.1.2 is a maintenance release. No happy-path behavior change: it corrects the default of `HERMES_ORACLE_SEARCH_TIMEOUT_MS` from a 10x typo (`1300` = 1.3 s) to the 13 s budget the read path actually enforces, collapses the duplicated environment reads inside `initialize()` so the pool fingerprint and the pool construction can never disagree, and fixes two documentation inaccuracies (ADB sample DSN port and the env-variable name shown in the env tables).

### Why
1. **10x typo in the pool-timeout default** — `initialize()` read `HERMES_ORACLE_SEARCH_TIMEOUT_MS` with default `"1300"` (1.3 s) while every other surface of the plugin documents and enforces a 13 s budget: the read-path clamp (13.0 s, derived from `HERMES_ORACLE_SEARCH_TIMEOUT`), the test suite (which asserts the 13000 ms values), and the README/INSTALL env tables. The typo was inert in practice only because `pool_call_timeout = max(search_ms, ingest_ms)` is dominated by the 600 s ingest default — but the fingerprint hash and the pool's declared budget silently disagreed with the documented 13 s, and any deployment that lowered the ingest timeout would have exposed an actual 1.3 s search budget.
2. **Duplicated environment reads** — the same three values (ingest ms, search ms, pool call-timeout) were read from the environment twice inside `initialize()`: once below the pool-reuse check (feeding the fingerprint) and again inside the `try:` immediately before pool construction. Two reads mean two moments in time; an env change observed between them would split the fingerprint from the budget actually declared. There is now a single read, used by both.
3. **Documentation inaccuracies** (no code change)
   - `INSTALL.md`'s ADB connection sample used port `1521`; the real TCP/TLS ADB port is `1522` (tcps).
   - The README/INSTALL env tables listed `HERMES_ORACLE_SEARCH_TIMEOUT` (default `1300`). The only env var the code reads on this path is `HERMES_ORACLE_SEARCH_TIMEOUT_MS` (default `13000`); the name without `_MS` is a module-level Python constant in seconds, not an env variable. The tables now name the variable the code actually reads.

### What changed
- `__init__.py`: `initialize()` default for `HERMES_ORACLE_SEARCH_TIMEOUT_MS` changed `"1300"` → `"13000"`; the duplicated three-line read block inside the `try:` removed, replaced by a comment noting the config is read once.
- `INSTALL.md`: sample DSN port 1521 → 1522; env table corrected to `HERMES_ORACLE_SEARCH_TIMEOUT_MS` / `13000`.
- `README.md`: env table corrected; "What's new in v1.1.1.2" section added.
- Version bump `1.1.1` → `1.1.1.2` across `VERSION`, `plugin.yaml`, `pyproject.toml`, `__version__`, README/INSTALL badges, and both `skills/*/SKILL.md` version pins.
- Release-notes rotation: `RELEASE_NOTES_1.1.1.md` → `RELEASE_NOTES_1.1.1.2.md` (single-current-notes convention). The v1.1.1 notes content is preserved in `CHANGELOG.md`.

### Notes
- **No pool-rebuild forced on the fleet**: under the stock 600 s ingest default, `pool_call_timeout = max(search_ms, ingest_ms)` is 600000 ms regardless of the search default, so the fingerprint value is unchanged and this release does not trigger a fleet-wide pool rebuild.
- Test suite: 241 passed, 2 skipped (the timeout tests already encoded the corrected 13000 ms values — they gated this fix).
- Credentials and identifiers remain scrubbed: the tree carries no fleet hostnames, operator identifiers, or production region/service descriptors.