---
name: oracle-skill-corpus
description: Discover and materialize shared cross-server skills from Oracle 26ai ADB memory at runtime without requiring local disk installation.
version: 1.1.1.2
author: Hermes Community
metadata:
  hermes:
    tags: [skills, corpus, oracle, shared, cross-server, discovery]
---

# Oracle 26ai Shared Skill Corpus

Discover and materialize skills from the fleet-wide shared Oracle 26ai Autonomous Database (`HERMES_AGENT_MEMORY`) skill corpus at runtime.

The shared corpus contains over 2,300+ skills synchronized across multiple Hermes server profiles and hosts. Any Hermes agent connected to Oracle 26ai can search, inspect, and reconstruct full skills dynamically—even if the skill files do not exist on the local disk.

## Core Rule

> **Never assume a skill is unavailable just because its file is missing locally.**
> Always check the shared Oracle skill corpus using `oracle_load_skill(action="list", filter="...")` before declaring a capability missing.

## When to Use

- A user requests a capability or task, and no local skill in `~/.hermes/skills/` or `~/.agents/skills/` matches.
- Exploring available workflows, domain knowledge, or integrations across the shared fleet library.
- Dynamically loading instructions and reference documents for an uninstalled skill.
- Resolving conflicting skill versions or inspecting cross-host variants.

## How to Use `oracle_load_skill`

The `oracle_load_skill` tool provides two primary operations:

### 1. Discovering Skills (`action="list"`)

Search the shared catalog by skill name or description substring:

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
- `count_in_page`: number of skills returned (up to 500 per page)
- `offset`: current pagination offset
- `more`: boolean indicating if additional pages are available
- `skills`: list of `{name, category, description}`

### 2. Loading and Reconstructing a Skill (`action="fetch"`)

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

When multiple servers have contributed variants of the same skill with different paths, `fetch` returns a `variants` manifest:

```json
{
  "status": "multiple_variants",
  "variants": [
    {"variant": 0, "path": "/srv/agent-a/.hermes/skills/example/sample/SKILL.md", "hash": "abc..."},
    {"variant": 1, "path": "/Users/agent-b/.hermes/skills/example/sample/SKILL.md", "hash": "def..."}
  ]
}
```

Specify either the `variant` index or `path` to select a specific variant:

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
- Zombie-safe pruning: deleting or moving a skill locally prunes only this host's rows, leaving foreign host skills untouched.
