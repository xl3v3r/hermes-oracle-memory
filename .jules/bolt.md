## 2024-05-13 - N+1 Connection Pool Acquisitions in Batch Chunking
**Learning:** During bulk text processing (e.g. `_chunk_and_store`), grabbing a connection from the pool per text segment inside a loop rather than pulling one outside the loop is an anti-pattern. Doing so acquires and releases DB connections in an N+1 manner unnecessarily.
**Action:** Always acquire a single connection (`with self._pool.acquire() as conn:`) and single cursor in the outer scope, and pass the cursor down to the lower-level functions when executing batch operations or iterating over many items.
