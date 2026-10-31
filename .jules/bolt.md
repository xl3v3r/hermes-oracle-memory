## 2024-05-15 - [Database N+1 Query in Retrieval Ranker]
**Learning:** During semantic memory retrieval, the `_apply_weights` function was doing an additional lookup query for `created_at` for every fetched item (in chunks), despite the rows having just been fetched from the database.
**Action:** Always project required fields in the initial retrieval queries instead of performing late-stage secondary queries during ranking or weighting stages.
