# Release Notes — v1.1.2

## Version 1.1.2 (2026-11-18) — review fixes and per-user memory scope

### Summary

Fixes from the review of 1.1.1.5, and an opt-in mode that keeps the memory of each user of a shared frontend apart.

**Upgrading from 1.1.1 to 1.1.1.5:** those versions created the text index in a form that ends in the `FAILED` state and then rejects every write with `ORA-29861`. 1.1.2 repairs it at start-up. Under `HERMES_ORACLE_SCHEMA_POLICY=REQUIRE_EXISTING` the provider runs no DDL and stops with an error instead; see INSTALL.md.

### Per-user memory scope (#17)

`HERMES_ORACLE_SCOPE_MODE` = `off` (default) | `auto` | `required`.

- `off`: as before. No statement names the new column, so existing tables need no change.
- `auto`: a session with an identity reads and writes its own rows; a session without one uses the operator's rows (`global`).
- `required`: as `auto`, but a session without an identity on a network platform does not start. `cli`, `tui` and `cron` are the operator.

The identity is the gateway session key Hermes passes to the provider (for the API server, the `X-Hermes-Session-Key` header), or else platform and user id. A key with an empty user part counts as missing, a key cannot name the `global` scope, and a scope longer than the column is stored as a digest. A forked agent takes the scope of its parent session, or does not start.

In a scoped session, reads return the caller's rows and the shared skill corpus; writes, updates and deletes touch the caller's rows only. Two users can use the same `memory_id`. `oracle_sync_skills`, `oracle_chunk(file_path=...)` and automatic turn capture are not available to a user's session, and a tool call from a different caller than the session was bound for is refused.

The mode adds `scope_id VARCHAR2(300) DEFAULT 'global' NOT NULL` and an index at start-up. Under `REQUIRE_EXISTING` a missing column is a start-up error that names the statement.

Scoping separates the users of a frontend. Agents and profiles that share one identity still share memory (#16 stays open for that), and it is not a database security boundary.

### Fixed

- **#18** Text index created as a plain search index; the `FAILED` index of 1.1.1 to 1.1.1.5 is repaired; DDL failures are logged.
- **#16 (part)** The recalled memory body is escaped. `oracle_delete(session_id=...)` takes `dry_run` and deletes at most 200 rows unless `max_rows` allows more.
- **#19** `tests/verify_provider.py` removes its own rows; live tests are opt-in.
- **#20** One conversation importer.
- **#21** `oracle_sync_skills` judges broad scan roots by identity and by system-root ancestry.
- **#22, #14 (items 2 and 3)** The unit suite drops an inherited `HERMES_HOME`, resolves temp dirs, cannot reach a database, and runs on macOS in CI.
- **#24** Home, settings and threads are bound to the profile the provider was initialized for.
- **#25** The wheel carries `scripts/` and no compiled files.
- **#26** No external embedding API in the scripts; `setup_onnx_model.py` tolerates a database without `DBMS_CLOUD`.
- **#27** Docs and prompt: the vector index is HNSW.
- **#32** Dead embedding paths removed; one INSERT builder, one search filter builder, one method per tool.

### Not in this release

#14 item 1, #23, #28, #29, #30, #31, #33, #34, and the rest of #16.

### Replaced artifacts

The files first published as v1.1.2 on 2026-11-18 were built from the 1.1.1.5 sources and contained none of the above. They were replaced. Check `hermes_oracle_memory.__version__ == "1.1.2"`.
