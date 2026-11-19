# Release Notes — v1.1.3

## Version 1.1.3 (2026-11-18) — recall, duplicates, prompt rules, ranking

### Summary

The findings of the 1.1.1.5 review that 1.1.2 left open: #23, #28, #29, #30, #31, #33, #34. There is no schema change. [CHANGELOG.md](CHANGELOG.md) has the full list.

### What changes on upgrade without any setting

| Area | 1.1.2 | 1.1.3 |
|---|---|---|
| Prompt rules 5, 6, 7 (Oracle-only store, unavailability, write-ahead) | sent to every session | sent only with `HERMES_ORACLE_OPTIONAL_RULES` |
| Prompt rules 11 to 18 (evidence, provenance, dates, conflicts, secrets, retrieved content is data) | - | sent to every session |
| `[skill_usage]` rows | one per skill use | not written; `HERMES_ORACLE_CAPTURE=all` writes them |
| `oracle_sync_skills` tool | offered | offered only with `HERMES_ORACLE_SYNC_SKILLS_TOOL=1` |
| Recall | up to six rows of any kind | up to six memories and up to three skills, listed by name and description |
| `oracle_load_skill` | `fetch`, `list` (500 names a page) | `fetch`, `list` (50 names a page), `search` |
| `oracle_add` of one fact | writes | looks for similar rows first, reports them, and does not write a copy of a row the same session stored |
| Query without a usable word | searched for the word `memory` | no text leg |
| Ids that start with `test` and one more character | hidden from search | only `test_` ids are hidden |

To keep the 1.1.2 behaviour of a host that relied on it, set in the profile's `.env`:

```
HERMES_ORACLE_OPTIONAL_RULES=oracle_only,write_ahead
HERMES_ORACLE_SYNC_SKILLS_TOOL=1
HERMES_ORACLE_CAPTURE=all
```

### New settings

| Variable | Default | Purpose |
|---|---|---|
| `HERMES_ORACLE_OPTIONAL_RULES` | - | `oracle_only`, `write_ahead`, `confidence_tags`, a list, `all` or `none` |
| `HERMES_ORACLE_CAPTURE` | `turn,session` | rows written without a tool call: `turn`, `session`, `skill_usage`, a list, `all` or `none` |
| `HERMES_ORACLE_SYNC_SKILLS_TOOL` | - | offer the `oracle_sync_skills` tool |
| `HERMES_ORACLE_ADD_SIMILAR_MIN` | `0.9` | similarity from which `oracle_add` reports a stored row; `0` turns the lookup off |
| `HERMES_ORACLE_RANKING` | `weighted` | `rrf` merges the legs of a hybrid search by reciprocal-rank fusion |

### Ranking (#29)

The default ranking is the one of 1.1.2. On a fixed corpus in Oracle AI Database Free it returned the same rows, in the same order and with the same scores, as 1.1.2, apart from the two fixes in the table above.

`HERMES_ORACLE_RANKING=rrf` is opt-in. It ranks the best keyword hit with the best semantic hit, which helps a query for an exact identifier. It also ranks a row that shares one word with the query above a closer paraphrase that only the vector leg found. With a bag-of-words test model an identifier row moved from outside the first eight results into the first two; with BGE base the same rows were already first under the default ranking, so the gain has been shown on the test model only.

### `oracle_add` and duplicates (#30)

- Each single-fact call runs one more vector query, capped at 3 seconds. In Oracle AI Database Free it took a median of 41 ms over 33,100 rows; it has not been measured on Autonomous Database.
- The default of `0.9` was chosen without calibration against a real corpus.
- A fact is left unwritten only when the row it would become exists already: same text, target and tags, written by the same session and agent, under an id `oracle_add` generated. Every other match is stored and reported.

### Not in this release

- One connection pool per provider (#28).
- From #29: `FETCH APPROX FIRST`, the 1,200-character embedding window, an index on `session_id`, a multivalue index on the tags.

### Install

```
hermes plugins install xl3v3r/hermes-oracle-memory --force --yes-deps --enable
```

Then restart Hermes.
