# Release Notes — v1.1.1

## Version 1.1.1 (2026-11-07) — Sterile Distribution: Identifier Scrub + clean install-scan verdict

### Summary
`hermes-oracle-memory` v1.1.1 is a distribution-hygiene release. No behavioral changes to the provider, sync, or search: it removes every environment-specific and person-specific identifier from the shipped tree so the plugin is sterile — installable on any host without leaking its origin — and so it clears the Hermes install-time security scanner with a **`safe` verdict (zero critical, zero high findings)** instead of a `caution` verdict that required confirmation.

### Why
The v1.1.0.1 tree was a `caution` verdict on the in-process plugin security scan (plugin-guard-v12) because of two HIGH-severity findings:

1. **`.github/workflows/cla.yml`** — GitHub-Expressions interpolation inside a markdown link (`[${{ github.repository }}]`) matched the `md_link_exfil` ("markdown link with variable interpolation → exfiltration") pattern. That pattern is exactly what the scanner flags as a prompt-injection/exfil trip-wire on docs and workflow files, and it fired here on a harmless CLA bot comment.
2. **`INSTALL.md`** — the DB-connection example carried a credential-shaped placeholder (a 24-character camelCase value assigned to `OCI_DB_PASSWORD=`), which matched `hardcoded_secret`. The example now uses `<…>` placeholders.

Verdict mapping (Hermes installer): any critical → `dangerous` (hard block, `--force` cannot override); any high → `caution` (install requires user confirmation — the "multiple warnings" the UI showed); medium/low alone → `safe` (informational only, no prompt).

### What changed
1. **Removed the two HIGH findings**
   - `cla.yml`: dropped the `custom-notsigned-prcomment` block (the flagged pattern) and pointed `path-to-document` at the plain in-repo `CLA.md` file (no URL interpolation). The CLA bot keeps its allowlist and signature tracking; only the decorative comment text is gone.
   - `INSTALL.md`: the DB-connection example now uses placeholder values (`<your-db-user>`, `<your-db-password>`, `adb.<your-region>.oraclecloud.com`, `<service-name>`) — no credential-shaped literals, no real region/service hostnames.
2. **Identifier scrub across the shipped surface** (no behavior change; docs and test fixtures only)
   - Fleet hostnames (the two deployment hosts + their domain + the decommissioned third host) removed from release notes, changelog, and tests; replaced by role-neutral terms ("seeding host", "the second host").
   - Person / account identifiers (system usernames, a contributor's macOS account, and absolute build/home paths under those users) removed from all tracked files; test fixtures now use synthetic hosts (`hosta`/`hostb`/`hostc`), synthetic user names, and fixture roots `/srv/fixtures/` / `/Users/dev/`.
   - The production OCI region + ADB service descriptors replaced with generic placeholders in docs and in the OCI GenAI fallback default region (still overridable via `OCI_GENAI_REGION` / signer `region`; the default only matters for that disabled-by-default fallback path).
   - A guessed default DB-user value in README/INSTALL/skill docs reworded to a factual statement ("ADB's default account is `ADMIN`") with the env example on placeholders.
   - Shipped skill `oracle-skill-corpus` example variant paths (real user home dirs + a real skill name) replaced with synthetic paths/names.
3. **Install-scan result (measured with the actual installer scanner, plugin-guard-v12)**
   - Before: **`caution`** — 32 findings, 2 high.
   - After: **`safe`** — 31 findings, **0 critical, 0 high**; all 31 are medium/low informational (expected plugin behavior: reading its own `~/.hermes/.env`, `git clone` of the project itself, unpinned `pip install` of its own deps, `subprocess` interpreter probe, deliberate hostile-string fixtures in tests that exist to prove the provider rejects them). These do not prompt and do not gate installation.
   - `should_allow_plugin_install` → `("Allowed (clean scan)")` for a fresh install.

### Notes
- Credentials were never present in the tree (verified: no `.env`, no key blobs, no token shapes across the 50 tracked files — the earlier `hardcoded_secret` hit was a 24-char placeholder, not a secret). This release removes the *appearance* of one plus all origin identifiers.
- Git *history* still contains the pre-1.1.1 identifiers (40 commits). Clones of the repo can see them; the **installed/distributed tree** is what the scanner reads and it is clean. If you need zero leak-in-history, that requires a history rewrite (or a fresh repo) — not done here.
- Test suite: 241 passed, 2 skipped (identical to v1.1.0.1; the fixture renames are assertion-consistent).