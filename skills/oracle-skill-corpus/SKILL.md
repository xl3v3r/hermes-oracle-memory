---
name: oracle-skill-corpus
description: Use when a needed skill is not installed locally.
version: 1.1.301
author: Hermes Community
metadata:
  hermes:
    tags: [skills, corpus, oracle, shared, cross-server, discovery]
---

# Oracle 26ai Shared Skill Corpus

Discover and materialize skills from the shared skill corpus in the Oracle memory table at runtime.

The corpus holds the skills that every Hermes host and profile connected to the same database has synchronized. Any agent connected to it can search, inspect, and reconstruct full skills, even if the skill files do not exist on the local disk.

## Core Rule

> **Never assume a skill is unavailable just because its file is missing locally.**
> Always check the shared Oracle skill corpus using `oracle_load_skill(action="search", query="...")` before declaring a capability missing.

## When to Use

- A user requests a capability or task, and no local skill in `~/.hermes/skills/` or `~/.agents/skills/` matches.
- Exploring available workflows, domain knowledge, or integrations across the shared corpus.
- Dynamically loading instructions and reference documents for an uninstalled skill.
- Resolving conflicting skill versions or inspecting cross-host variants.

## How to Use `oracle_load_skill`

The `oracle_load_skill` tool provides three operations:

### 1. Finding Skills (`action="search"`)

Describe the task; the response holds up to eight skills, best match first, as `{name, description, score}`:

```json
{
  "action": "search",
  "query": "validate a US postal address"
}
```

Recall may already name matching skills in a turn's context, in `<retrieved_skill name="...">` lines. Skills installed in this profile are not listed there.

### 2. Browsing Skills (`action="list"`)

Filter the catalog by a substring of the skill name or description:

```json
{
  "action": "list",
  "filter": "address"
}
```

Or paginate through the entire corpus:

```json
{
  "action": "list",
  "offset": 0
}
```

The response includes:
- `count_in_page`: number of skills returned (up to 50 per page)
- `offset`: current pagination offset
- `more`: boolean indicating if additional pages are available
- `skills`: list of `{name, category, description}`

### 3. Loading and Reconstructing a Skill (`action="fetch"`)

Retrieve and reconstruct the full skill markdown and reference documents from in-database chunks:

```json
{
  "action": "fetch",
  "skill": "usps-address-validator"
}
```

The tool reconstructs:
- `markdown`: the full SKILL.md body reassembled from word chunks (lossless at word level)
- `references`: dictionary of associated reference documents (`{rel_path: content}`)
- `category`: skill category
- `source_host_path`: source path from the originating server
- `content_hash`: SHA256 hash of the original SKILL.md
- `word_count` & `chunk_count`: verification statistics

#### Multi-Variant Handling

When several hosts (or paths) hold a copy of the same skill, `fetch` returns a `variants` manifest:

```json
{
  "status": "multiple_variants",
  "variants": [
    {"variant": 0, "host": "agent-a", "path": "/srv/agent-a/.hermes/skills/example/sample/SKILL.md", "hash": "abc..."},
    {"variant": 1, "host": "agent-b", "path": "/Users/agent-b/.hermes/skills/example/sample/SKILL.md", "hash": "def..."}
  ]
}
```

Specify the `variant` index, or `host` and/or `path`, to select a specific variant (the same path on two hosts needs `host`):

```json
{
  "action": "fetch",
  "skill": "sample",
  "variant": 0
}
```

## Storage & Ingestion Model

- Skills are indexed into the shared `HERMES_AGENT_MEMORY` table under `role="skill"`.
- Chunks preserve exact word sequences with 10-word boundary overlaps.
- Live updates: creating, editing, patching, or installing a skill locally immediately synchronizes it to Oracle via the `on_skill_lifecycle` hook.
- Rows are `skill:<host>:<name>:c<N>`; a host only replaces or deletes its own copy. Host-less legacy rows are the shared corpus and are never pruned.
- Pruning is opt-in (`python3 scripts/sync_skills.py --prune`, preview with `--dry-run`) and removes only this host's skills that are gone from disk. The `oracle_sync_skills` tool takes the same options; a session has it only when the profile sets `HERMES_ORACLE_SYNC_SKILLS_TOOL=1`.
- A sync result with `skipped_foreign` > 0 lists copies stored from SKILL.md files outside this profile's skill dirs (another profile, or a removed dir). They are kept; `replace_foreign=true` replaces them with this profile's copies.
