## 2024-11-11 - Project `created_at` in Initial Retrieval
**Learning:** Performing a secondary query in the weighting stage (`_apply_weights`) to retrieve `created_at` values creates an unnecessary performance bottleneck and can cause an N+1 queries-like pattern.
**Action:** Always project required fields (such as `created_at`) directly in the initial retrieval SELECT statements (`_build_text_search_sql`, `_build_vector_search_sql`, `_build_hybrid_search_sql`) to avoid additional round trips during ranking or weighting stages.
