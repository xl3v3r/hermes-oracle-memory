## 2024-10-08 - Batching database updates in Oracle background worker
**Learning:** The background embedding worker (`_embed_worker_loop`) batched embeddings correctly from the API but suffered from an N+1 database query anti-pattern by executing 32 individual `UPDATE` statements sequentially.
**Action:** Always verify that batched API requests are also batched when writing to the database by using `executemany` instead of iterating over `execute`.
## 2024-10-09 - Optimize Oracle LOB fetching
**Learning:** Fetching Oracle CLOB/LOB columns returns LOB locators by default, requiring an additional network round-trip per row when `.read()` is called (the N+1 queries problem).
**Action:** Use an `outputtypehandler` on the `cursor` to map `oracledb.DB_TYPE_CLOB` to `oracledb.DB_TYPE_LONG`. This safely fetches CLOB contents directly as strings for specific cursors without mutating global state, eliminating the N+1 network round-trips.
## 2024-10-10 - Optimize Oracle LOB fetching in cur.callfunc
**Learning:** Similarly to `.execute()`, when using `cur.callfunc()` that returns a CLOB/LOB, passing `str` as the return type parameter will yield a LOB locator, which later requires an N+1 network round-trip via `.read()`.
**Action:** Use `oracledb.DB_TYPE_LONG` directly as the return type parameter in `cur.callfunc()` to fetch the contents as a Python string directly in one round-trip.
