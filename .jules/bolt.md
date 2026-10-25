
## 2024-05-15 - [Avoid N+1 Connection Pool Acquisitions]
**Learning:** In batch operations like processing multiple conversation turns (`_chunk_and_store`) or handling multiple facts at once (`oracle_add`), obtaining a database connection cursor per item causes extreme slow-downs (N+1 connection problem) due to the overhead of acquiring connection pools repeatedly for the same task block.
**Action:** Always acquire a single connection/cursor in the outer scope of the batch logic and pass it down to inner functions, leveraging batch insert methods (like `_insert_many`) instead of looping over single inserts.
