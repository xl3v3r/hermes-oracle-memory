## 2024-10-27 - Avoid N+1 connection pool acquisitions in loops
**Learning:** During conversation chunking, `_chunk_and_store` processed turns iteratively. Inside the loop, it called `_chunk_text`, which redundantly acquired a new database connection from the pool on every iteration for `UTL_TO_CHUNKS`. This N+1 pattern caused severe pool exhaustion on lengthy conversations.
**Action:** Always acquire the connection and cursor outside of batch loops when processing multiple entries to avoid exhausting the DB pool, and pass the cursor down to the helper functions.
