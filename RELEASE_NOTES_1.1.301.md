# Release Notes — v1.1.301

## Version 1.1.301 (2026-11-20) — the `oracle` toolset, `oracle_only` enforcement, operator scripts

### Summary

#38: an `oracle_only` deployment disables the `memory` toolset to remove the
built-in `MEMORY.md` tool. Hermes surfaces memory-provider tools only while the
`memory` toolset is on, so the `oracle_*` tools vanished with it — and, because
the provider's system-prompt block rides the same gate, so did the memory rules.
There is no schema change. [CHANGELOG.md](CHANGELOG.md) has the full list.

### What changes on upgrade without any setting

| Area | 1.1.3 | 1.1.301 |
|---|---|---|
| `oracle_*` tools when the `memory` toolset is disabled | vanish | stay, under the plugin's own `oracle` toolset (default-on) |
| Memory rules when the `memory` toolset is disabled | withheld | injected on the first turn by a `pre_llm_call` hook |
| Built-in `memory` tool under `HERMES_ORACLE_OPTIONAL_RULES=oracle_only` | offered | blocked by a `pre_tool_call` hook |
| `write_file` / `patch` of `MEMORY.md` and `USER.md` under `oracle_only` | possible | blocked |
| Operator shell access | `scripts/sync_skills.py` and friends | plus `scripts/doctor.py` (diagnostics) and `scripts/memory_cli.py` (add / search / get / delete) |

The two hooks are new but inert unless the situation calls for them:
`pre_llm_call` injects nothing while the block is present, and `pre_tool_call`
does nothing unless `oracle_only` is in `HERMES_ORACLE_OPTIONAL_RULES`.

### The `oracle` toolset

Hermes resolves plugin toolsets separately from the built-in `memory` toolset,
so a plugin toolset named `oracle` is on by default for a platform until the
operator saves a toolset list that omits it. The `oracle_*` tools are now also
registered there (once per process), which is what keeps them on a platform
whose `memory` toolset is disabled.

- Disable the toolset with `hermes tools disable oracle` (add `--platform <p>` to
  target another platform; `hermes tools enable oracle` turns it back on).
- Re-enable the built-in `memory` toolset with `hermes tools enable memory`;
  with it on, Hermes surfaces the provider tools through the memory-provider
  path as before, and the `oracle` toolset copy is redundant but harmless.
- A user-scoped session with the `memory` toolset off lists all eight tools but
  the calls are refused, because the surface lists the schemas and the per-session
  provider that would serve them is not active. This is a known limitation of
  listing schemas without a live provider, not a security hole.

### Operator scripts

- `python3 scripts/doctor.py --env-file <path>` reports the version, whether the
  driver imports, which `OCI_DB_*` settings are set (names only, never values),
  a `SELECT 1` probe, and the toolset / memory gate when a Hermes config is
  readable. Exit 0 when healthy.
- `python3 scripts/memory_cli.py add|search|get --env-file <path> [--table T]`
  runs the same handlers the tools use, out of process, for operators who need
  shell access to the store. `--delete` removes a row.

### Not in this release

- Surfacing live per-session tool availability while the `memory` toolset is
  off (the schema-listing limitation above).

### Install

```
hermes plugins install xl3v3r/hermes-oracle-memory --force --yes-deps --enable
```

Then restart Hermes, and the gateway if it runs as a service.
