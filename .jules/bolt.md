## 2024-05-24 - [Oracle SARGable Query Tuning]
**Learning:** Using `NVL(col, default) <> target` in Oracle queries prevents the optimizer from using an index on `col` because it requires evaluating the `NVL` function on every row.
**Action:** Rewrite the condition to be SARGable, such as `(col IS NULL OR col <> target)`, and ensure an index is created on `col` for fast retrieval.
