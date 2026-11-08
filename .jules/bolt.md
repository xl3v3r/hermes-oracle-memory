## 2024-11-08 - Eliminate N+1 query in _apply_weights ranking
**Learning:** During ranking/weighting stages, executing secondary DB queries (such as fetching `created_at` in chunked sub-queries for retrieved IDs) acts as a hidden N+1 query pattern, which can severely impact performance.
**Action:** Always project all necessary fields (e.g. `created_at`) directly in the initial retrieval SELECT statements (text, vector, and hybrid search queries) so no extra trips to the database are needed during the ranking phase.
