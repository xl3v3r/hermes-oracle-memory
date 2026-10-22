## 2024-03-24 - Journal Created
**Learning:** Initial journal setup.
**Action:** Ready to log.

## 2024-03-24 - Batch Insert for Array Prompts
**Learning:** In synchronous LLM tool executions like `oracle_add`, processing an array parameter sequentially through a single-row DML function (`_insert`) incurs massive N+1 round-trip penalties.
**Action:** When handling `facts: list[str]`, convert the input immediately into a row format and route through a batch-enabled method (`_insert_many` using `executemany`) to avoid UI-blocking N+1 delays.
