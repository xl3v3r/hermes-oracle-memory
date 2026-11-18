"""1.1.1.4 recall path (#10): timeout classification, LOB-free fetch, fetch
sizing, fresh cursor per ORA-51815 retry, ranking before the top_k cut, and
consistent hybrid scores. Fully mocked; no database."""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401  (stubs agent.* / tools.*)
import __init__ as oracle_plugin  # noqa: E402


def _provider():
    p = oracle_plugin.OracleMemoryProvider()
    p._table = "HERMES_AGENT_MEMORY"
    p._onnx_model = "BGE_BASE_EN_V1_5"
    conn, cur = MagicMock(), MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    p._pool = MagicMock()
    p._pool.acquire.return_value = conn
    return p, conn, cur


def _db_row(mid, txt=0.0, dist=0.2, content=None, conv_ref=None):
    return (mid, "s", "a", "fact", content or f"content of {mid} long enough to avoid the short penalty",
            "memory", txt, dist, None)


class TestTimeoutClassification(unittest.TestCase):
    def test_codes_and_python_timeouts(self):
        t = oracle_plugin._is_timeout_error
        self.assertTrue(t(TimeoutError("x")))
        self.assertTrue(t("DPY-4024: call timeout of 13000 ms exceeded"))
        self.assertTrue(t("DPY-4005: timed out waiting for the connection pool to return a connection"))
        self.assertTrue(t("ORA-01013: user requested cancel of current operation"))
        self.assertTrue(t("socket read timed out"))

    def test_ora_messages_mentioning_timeout_are_not_search_timeouts(self):
        t = oracle_plugin._is_timeout_error
        self.assertFalse(t("ORA-04021: timeout occurred while waiting to lock object"))
        self.assertFalse(t("ORA-12170: TNS:Connect timeout occurred"))
        self.assertFalse(t("ORA-40284: model does not exist"))
        # ORA-12170 is still a broken-session error (retried on a fresh connection).
        self.assertTrue(oracle_plugin._is_session_error("ORA-12170: TNS:Connect timeout occurred"))

    def test_hybrid_does_not_report_lock_timeout_as_search_timeout(self):
        p, _, _ = _provider()
        cur = MagicMock()
        cur.execute.side_effect = RuntimeError("ORA-04021: timeout occurred while waiting to lock object")
        with self.assertRaises(RuntimeError) as cm:
            p._execute_hybrid_search(cur, "q", "{q}", 10, "any")
        self.assertNotIsInstance(cm.exception, TimeoutError)


class TestFetch(unittest.TestCase):
    def test_lobs_fetched_as_str_and_array_covers_fused_rows(self):
        p, _, _ = _provider()
        cur = MagicMock()
        cur.fetchall.return_value = [_db_row("m1")]
        p._fetch_rows(cur, "SELECT", {"k": 40}, max_rows=80)
        self.assertEqual(cur.execute.call_args.kwargs, {"fetch_lobs": False})
        self.assertEqual(cur.arraysize, 81)
        self.assertEqual(cur.prefetchrows, 81)

    def test_lob_fallback_for_drivers_without_fetch_lobs(self):
        cur = MagicMock()
        calls = []

        def _exec(sql, binds, **kw):
            calls.append(kw)
            if kw:
                raise TypeError("execute() got an unexpected keyword argument 'fetch_lobs'")

        cur.execute.side_effect = _exec
        oracle_plugin._execute_lobs_as_str(cur, "SELECT", {})
        self.assertEqual(calls, [{"fetch_lobs": False}, {}])

    def test_lob_values_still_read(self):
        p, _, _ = _provider()
        cur = MagicMock()
        lob = MagicMock()
        lob.read.return_value = "full text"
        cur.fetchall.return_value = [("m1", "s", "a", "fact", lob, "memory", 0.0, 0.1, None)]
        self.assertEqual(p._fetch_rows(cur, "SELECT", {"k": 1})[0]["content"], "full text")

    def test_51815_retry_uses_fresh_cursor(self):
        p, _, _ = _provider()
        cur = MagicMock()
        cur.execute.side_effect = RuntimeError("ORA-51815: snapshot too old")
        fresh = MagicMock()
        fresh.fetchall.return_value = [_db_row("m1")]
        cur.connection.cursor.return_value.__enter__.return_value = fresh
        with patch.object(oracle_plugin.time, "sleep"):
            rows = p._execute_hybrid_search(cur, "q", "{q}", 10, "any")
        self.assertEqual([r["memory_id"] for r in rows], ["m1"])
        cur.execute.assert_called_once()
        fresh.execute.assert_called_once()

    def test_get_rows_caps_ids_and_fetches_str(self):
        p, _, cur = _provider()
        with self.assertRaises(ValueError):
            p._get_rows(memory_ids=[f"m{i}" for i in range(oracle_plugin._GET_MAX_LIMIT + 1)])
        cur.fetchall.return_value = []
        p._get_rows(memory_ids=["a"])
        self.assertEqual(cur.execute.call_args.kwargs, {"fetch_lobs": False})


class TestRanking(unittest.TestCase):
    def test_hybrid_returns_whole_candidate_list(self):
        p, _, _ = _provider()
        cur = MagicMock()
        cur.fetchall.return_value = [_db_row(f"m{i}", dist=0.1 + i / 100) for i in range(12)]
        rows = p._execute_hybrid_search(cur, "q", "{q}", 10, "any")
        self.assertEqual(len(rows), 12)

    def test_diversification_cannot_shrink_below_top_k(self):
        p, conn, cur = _provider()
        # 6 chunks of one skill rank first, then 6 distinct facts.
        skill = [{"memory_id": f"skill:x:c{i}", "content": "skill chunk text that is long enough", "score": 0.9 - i / 100,
                  "role": "skill", "conv_ref": "skill:x"} for i in range(6)]
        facts = [{"memory_id": f"f{i}", "content": "fact text that is long enough to count", "score": 0.5 - i / 100,
                  "role": "fact"} for i in range(6)]
        with patch.object(p, "_execute_hybrid_search", return_value=skill + facts), \
                patch.object(oracle_plugin, "_source_key", side_effect=lambda r: r.get("conv_ref")), \
                patch.object(oracle_plugin, "_MAX_CHUNKS_PER_SOURCE", 2), \
                patch.object(p, "_rerank", side_effect=lambda q, rows, k, cur=None: rows) as rr:
            out = p._search_once("q", top_k=5)
        self.assertEqual(len(out), 5)
        self.assertEqual(sum(1 for r in out if r.get("conv_ref") == "skill:x"), 2)
        # rerank sees the weighted, diversified list, not a top_k cut
        self.assertEqual(len(rr.call_args[0][1]), 8)

    def test_rerank_reorders_without_truncating(self):
        p, _, _ = _provider()
        cur = MagicMock()
        cur.callfunc.return_value = '[{"id": "c"}, {"id": "a"}]'
        rows = [{"memory_id": m, "content": m} for m in ("a", "b", "c", "d")]
        out = p._rerank("q", rows, 2, cur=cur)
        self.assertEqual([r["memory_id"] for r in out], ["c", "a", "b", "d"])

    def test_cooldown_text_hits_use_hybrid_text_weight(self):
        p, _, _ = _provider()
        p._onnx_failed_at = time.monotonic()
        cur = MagicMock()
        cur.fetchall.return_value = [_db_row("m1", txt=100.0, dist=None, content="unrelated words here")]
        rows = p._execute_hybrid_search(cur, "q", "{q}", 10, "any")
        self.assertAlmostEqual(rows[0]["score"], 0.3, places=4)

    def test_degraded_text_hits_use_hybrid_text_weight(self):
        p, _, _ = _provider()
        snap = RuntimeError("ORA-51815: snapshot too old")
        text_rows = [{"memory_id": "m1", "score": 1.0, "content": "unrelated words", "dist": None}]
        with patch.object(oracle_plugin.time, "sleep"), \
                patch.object(p, "_fetch_rows", side_effect=[snap, snap, snap, text_rows]):
            rows = p._execute_hybrid_search(MagicMock(), "q", "{q}", 10, "any")
        self.assertAlmostEqual(rows[0]["score"], 0.3, places=4)


if __name__ == "__main__":
    unittest.main()
