## 2024-10-08 - Batching database updates in Oracle background worker
**Learning:** The background embedding worker (`_embed_worker_loop`) batched embeddings correctly from the API but suffered from an N+1 database query anti-pattern by executing 32 individual `UPDATE` statements sequentially.
**Action:** Always verify that batched API requests are also batched when writing to the database by using `executemany` instead of iterating over `execute`.
