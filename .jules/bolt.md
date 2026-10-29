
## 2024-05-18 - Avoid N+1 connection pool acquisitions
**Learning:** In the Oracle memory provider backend, calling `self._pool.acquire()` in inner loops (e.g. per text chunk) can lead to an N+1 connection pool acquisition pattern which degrades performance.
**Action:** When performing batched or looped DB operations, acquire a single connection outside the loop and pass it down to inner functions via dependency injection.
