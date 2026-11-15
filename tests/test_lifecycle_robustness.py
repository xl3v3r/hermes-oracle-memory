"""1.1.1.4: writes during the ONNX cooldown, backfill backoff and teardown (#7),
pool lifecycle (#8) and transactional insert error handling (#9).
Fully mocked; touches no real HOME and no database."""

import os
import sys
import threading
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
    cur.rowcount = 1
    p._pool = MagicMock()
    p._pool.acquire.return_value = conn
    return p, conn, cur


def _row(mid="m1", content="hello world"):
    return {"memory_id": mid, "session_id": "s", "role": "turn", "content": content,
            "target": "memory", "metadata": {}, "conv_ref": "c"}


class _StopEvent:
    """Records wait() calls instead of sleeping."""

    def __init__(self, stopping=False):
        self.stopping = stopping
        self.waits: list[float | None] = []

    def is_set(self):
        return self.stopping

    def wait(self, t=None):
        self.waits.append(t)
        return self.stopping

    def set(self):
        self.stopping = True

    def clear(self):
        self.stopping = False


# --------------------------------------------------------------------------- #9
class TestTxInsert(unittest.TestCase):
    def test_deadlock_is_a_data_error(self):
        self.assertFalse(oracle_plugin._is_onnx_model_failure("ORA-00060: deadlock detected while waiting for resource"))

    def test_failed_savepoint_rollback_aborts_tx(self):
        p, _, _ = _provider()
        tx, cur = MagicMock(), MagicMock()
        tx.cursor.return_value.__enter__.return_value = cur

        def _exec(sql, *a):
            if "ROLLBACK TO SAVEPOINT" in sql:
                raise RuntimeError("ORA-01086: savepoint never established")

        cur.execute.side_effect = _exec
        cur.executemany.side_effect = RuntimeError("ORA-51815: snapshot too old")
        with self.assertRaises(oracle_plugin._TxAborted):
            p._exec_insert("INSERT", [(1,)], tx)

    def test_tx_session_error_stops_ladder_and_skips_null_fallback(self):
        p, _, _ = _provider()
        tx, cur = MagicMock(), MagicMock()
        tx.cursor.return_value.__enter__.return_value = cur
        cur.executemany.side_effect = RuntimeError("ORA-03113: end-of-file on communication channel")
        with patch.object(oracle_plugin.time, "sleep"):
            with self.assertRaises(oracle_plugin._TxAborted):
                p._insert_many([_row()], tx_conn=tx)
        self.assertEqual(cur.executemany.call_count, 1)  # no retry, no NULL-embedding insert
        self.assertIsNone(p._onnx_failed_at)
        p._pool.acquire.assert_not_called()  # nothing moved to another connection

    def test_tx_51815_with_broken_savepoint_does_not_retry(self):
        p, _, _ = _provider()
        tx, cur = MagicMock(), MagicMock()
        tx.cursor.return_value.__enter__.return_value = cur
        cur.executemany.side_effect = RuntimeError("ORA-51815: snapshot too old")

        def _exec(sql, *a):
            if "ROLLBACK TO SAVEPOINT" in sql:
                raise RuntimeError("DPY-1001: not connected")

        cur.execute.side_effect = _exec
        with patch.object(oracle_plugin.time, "sleep"):
            with self.assertRaises(oracle_plugin._TxAborted):
                p._insert_many([_row()], tx_conn=tx)
        self.assertEqual(cur.executemany.call_count, 1)

    def test_tx_data_error_still_rolls_back_to_savepoint_and_raises(self):
        p, _, _ = _provider()
        tx, cur = MagicMock(), MagicMock()
        tx.cursor.return_value.__enter__.return_value = cur
        cur.executemany.side_effect = RuntimeError("ORA-00060: deadlock detected")
        with self.assertRaises(RuntimeError) as cm:
            p._insert_many([_row()], tx_conn=tx)
        self.assertNotIsInstance(cm.exception, oracle_plugin._TxAborted)
        self.assertTrue(any("ROLLBACK TO SAVEPOINT" in str(c.args[0]) for c in cur.execute.call_args_list))
        self.assertIsNone(p._onnx_failed_at)  # deadlock never trips the ONNX cooldown


# --------------------------------------------------------------------------- #7
class TestCooldownWrites(unittest.TestCase):
    def _cooling(self, p):
        p._onnx_failed_at = time.monotonic()

    def test_insert_during_cooldown_writes_null_row_and_queues(self):
        p, conn, cur = _provider()
        self._cooling(p)
        with patch.object(p, "_queue_embed_backfill") as q:
            p._insert(memory_id="m1", session_id="s", role="turn", content="hello", target="memory")
        sql = cur.execute.call_args[0][0]
        self.assertIn("NULL, SYSTIMESTAMP", sql)
        self.assertNotIn("VECTOR_EMBEDDING", sql)
        conn.commit.assert_called_once()
        q.assert_called_once_with("m1", "hello")

    def test_insert_without_model_is_policy_error(self):
        p, _, cur = _provider()
        p._onnx_model = None
        with self.assertRaises(oracle_plugin._PolicyError):
            p._insert(memory_id="m1", session_id="s", role="turn", content="hello", target="memory")
        cur.execute.assert_not_called()

    def test_insert_many_during_cooldown(self):
        p, _, cur = _provider()
        self._cooling(p)
        with patch.object(p, "_queue_embed_backfill") as q:
            self.assertEqual(p._insert_many([_row("a"), _row("b")]), [])
        self.assertNotIn("VECTOR_EMBEDDING", cur.executemany.call_args[0][0])
        self.assertEqual([c.args for c in q.call_args_list], [("a", "hello world"), ("b", "hello world")])

    def test_insert_many_during_cooldown_in_tx_returns_pairs(self):
        p, _, _ = _provider()
        self._cooling(p)
        tx, cur = MagicMock(), MagicMock()
        tx.cursor.return_value.__enter__.return_value = cur
        with patch.object(p, "_queue_embed_backfill") as q:
            out = p._insert_many([_row("a")], tx_conn=tx)
        self.assertEqual(out, [("a", "hello world")])
        q.assert_not_called()  # caller queues after its commit
        tx.commit.assert_not_called()

    def test_update_during_cooldown_stores_content_with_null_embedding(self):
        p, conn, cur = _provider()
        self._cooling(p)
        with patch.object(p, "_queue_embed_backfill") as q:
            n = p._update_row("m1", "new text")
        self.assertEqual(n, 1)
        sql = cur.execute.call_args[0][0]
        self.assertIn("embedding = NULL", sql)
        conn.commit.assert_called_once()
        q.assert_called_once_with("m1", "new text")

    def test_policy_error_does_not_drop_pooled_connection(self):
        p, conn, _ = _provider()
        with self.assertRaises(oracle_plugin._PolicyError):
            with p._acquire_checked():
                raise oracle_plugin._PolicyError("model not configured")
        p._pool.drop.assert_not_called()
        conn.close.assert_called_once()

    def test_plain_runtime_error_still_drops(self):
        p, _, _ = _provider()
        with self.assertRaises(RuntimeError):
            with p._acquire_checked():
                raise RuntimeError("'NoneType' object is not iterable")
        p._pool.drop.assert_called_once()


class TestBackfillWorker(unittest.TestCase):
    def setUp(self):
        self.p, self.conn, self.cur = _provider()
        self.stop = _StopEvent()
        self.p._write_stop = self.stop

    def test_queue_dedupes_memory_ids(self):
        with patch.object(self.p, "_ensure_embed_worker"):
            self.p._queue_embed_backfill("a", "x")
            self.p._queue_embed_backfill("a", "x")
            self.p._queue_embed_backfill("b", "y")
        self.assertEqual([m for m, _ in self.p._embed_pending], ["a", "b"])

    def test_failed_batches_back_off_then_drop(self):
        self.p._embed_pending.extend([("a", "x"), ("b", "y")])
        with patch.object(oracle_plugin, "_BACKFILL_MAX_ATTEMPTS", 4), \
                patch.object(self.p, "_embed_many_tagged", side_effect=RuntimeError("ORA-03113")):
            self.p._embed_worker_loop()
        self.assertEqual(self.stop.waits, [2.0, 4.0, 8.0])
        self.assertFalse(self.p._embed_pending)
        self.assertFalse(self.p._embed_attempts)

    def test_backoff_is_capped(self):
        self.p._embed_pending.append(("a", "x"))
        with patch.object(oracle_plugin, "_BACKFILL_MAX_ATTEMPTS", 12), \
                patch.object(self.p, "_embed_many_tagged", side_effect=RuntimeError("boom")):
            self.p._embed_worker_loop()
        self.assertEqual(max(self.stop.waits), oracle_plugin._BACKFILL_MAX_DELAY_SECS)

    def test_success_resets_and_updates(self):
        self.p._embed_pending.extend([("a", "x"), ("b", "y")])
        with patch.object(self.p, "_embed_many_tagged", return_value=[([0.1], "M"), ([0.2], "M")]):
            self.p._embed_worker_loop()
        self.cur.executemany.assert_called_once()
        self.assertEqual([b[2] for b in self.cur.executemany.call_args[0][1]], ["a", "b"])
        self.assertEqual(self.stop.waits, [])

    def test_waits_out_cooldown_without_spending_attempts(self):
        self.p._embed_pending.append(("a", "x"))
        self.p._onnx_failed_at = time.monotonic()

        def _wait(t=None):
            self.stop.waits.append(t)
            self.p._onnx_failed_at = None  # cooldown over
            return False

        self.stop.wait = _wait
        with patch.object(self.p, "_embed_many_tagged", return_value=[([0.1], "M")]) as emb:
            self.p._embed_worker_loop()
        self.assertEqual(len(self.stop.waits), 1)
        self.assertGreater(self.stop.waits[0], 0)
        emb.assert_called_once()

    def test_shutdown_drain_one_attempt_no_backoff(self):
        self.stop.stopping = True
        self.p._embed_pending.extend([("a", "x")])
        with patch.object(self.p, "_embed_many_tagged", side_effect=RuntimeError("boom")) as emb:
            self.p._embed_worker_loop()
        emb.assert_called_once()
        self.assertEqual(self.stop.waits, [])
        self.assertFalse(self.p._embed_pending)

    def test_shutdown_during_cooldown_leaves_rows_for_startup_scan(self):
        self.stop.stopping = True
        self.p._onnx_failed_at = time.monotonic()
        self.p._embed_pending.extend([("a", "x")])
        with patch.object(self.p, "_embed_many_tagged") as emb:
            self.p._embed_worker_loop()
        emb.assert_not_called()
        self.assertFalse(self.p._embed_pending)

    def test_requeue_null_embeddings(self):
        self.cur.fetchall.return_value = [("a", "x"), ("b", None), ("c", "z")]
        with patch.object(self.p, "_queue_embed_backfill") as q:
            self.assertEqual(self.p._requeue_null_embeddings(limit=7), 2)
        self.assertIn("embedding IS NULL", self.cur.execute.call_args[0][0])
        self.assertEqual(self.cur.execute.call_args[0][1], {"n": 7})
        self.assertEqual([c.args for c in q.call_args_list], [("a", "x"), ("c", "z")])

    def test_startup_scan_once_per_fingerprint_and_primary_only(self):
        self.p._pool_fingerprint = "fp-test-scan-%d" % id(self)
        self.p._agent_context = "subagent"
        with patch.object(self.p, "_requeue_null_embeddings") as rq:
            self.p._start_backfill_scan()
            self.assertIsNone(self.p._backfill_scan_thread)
            self.p._agent_context = "primary"
            self.p._start_backfill_scan()
            self.p._backfill_scan_thread.join(2)
            self.p._start_backfill_scan()  # same fingerprint: no second scan
        rq.assert_called_once()


# --------------------------------------------------------------------------- #8
class TestPoolLifecycle(unittest.TestCase):
    def test_retire_pool_closes(self):
        pool = MagicMock()
        oracle_plugin._retire_pool(pool, "replaced")
        pool.close.assert_called_once_with()

    def test_retire_busy_pool_defers(self):
        pool = MagicMock()
        pool.close.side_effect = RuntimeError("DPY-1005: unable to close pool with busy connections")
        with patch.object(oracle_plugin, "_close_pool_when_idle") as later:
            oracle_plugin._retire_pool(pool, "replaced")
            for t in threading.enumerate():
                if t.name == "oracle-pool-close":
                    t.join(2)
        later.assert_called_once_with(pool)

    def test_forced_close_is_logged(self):
        pool = MagicMock()
        pool.close.side_effect = [RuntimeError("DPY-1005 busy"), None]
        with self.assertLogs(oracle_plugin.logger, level="INFO") as cm:
            oracle_plugin._close_pool_when_idle(pool, budget=0.0, poll=0.0)
        self.assertTrue(any("forcing close" in m for m in cm.output))
        pool.close.assert_called_with(force=True)

    def test_reinit_with_new_fingerprint_retires_old_pool(self):
        p = oracle_plugin.OracleMemoryProvider()
        secrets = {"OCI_DB_USER": "ADMIN", "OCI_DB_PASSWORD": "ValidPassword123",
                   "OCI_DB_DSN": "dsn_a", "HERMES_ORACLE_TABLE": "hermes_agent_memory"}
        raw_a, raw_b = MagicMock(name="pool_a"), MagicMock(name="pool_b")
        with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, *a: secrets.get(k)), \
                patch("oracledb.SessionPool", side_effect=[raw_a, raw_b]), \
                patch.object(p, "_ensure_schema"), patch.object(p, "_start_backfill_scan"):
            p.initialize("s1")
            secrets["OCI_DB_DSN"] = "dsn_b"
            p.initialize("s2")
        raw_a.close.assert_called_once_with()
        raw_b.close.assert_not_called()
        p._pool = None

    def test_failed_reinit_retires_old_pool(self):
        p = oracle_plugin.OracleMemoryProvider()
        secrets = {"OCI_DB_USER": "ADMIN", "OCI_DB_PASSWORD": "ValidPassword123",
                   "OCI_DB_DSN": "dsn_a", "HERMES_ORACLE_TABLE": "hermes_agent_memory"}
        raw_a = MagicMock(name="pool_a")
        with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, *a: secrets.get(k)), \
                patch("oracledb.SessionPool", side_effect=[raw_a, RuntimeError("ORA-12541: no listener")]), \
                patch.object(p, "_ensure_schema"), patch.object(p, "_start_backfill_scan"):
            p.initialize("s1")
            secrets["OCI_DB_DSN"] = "dsn_b"
            p.initialize("s2")
        raw_a.close.assert_called_once_with()

    def test_busy_shutdown_closes_pool_when_writer_finishes(self):
        p, _, _ = _provider()
        pool = p._pool
        release = threading.Event()
        w = threading.Thread(target=release.wait, daemon=True)
        w.start()
        p._write_worker = w
        real_join = w.join
        with patch.object(w, "join", side_effect=lambda timeout=None: real_join(0.01)):
            p.shutdown()
        pool.close.assert_not_called()
        self.assertIs(p._pool, pool)
        release.set()
        for t in threading.enumerate():
            if t.name == "oracle-shutdown-finish":
                t.join(5)
        pool.close.assert_called_once_with()
        self.assertIsNone(p._pool)

    def test_deferred_finish_skips_rearmed_pool(self):
        p, _, _ = _provider()
        pool = p._pool
        p._write_stop.clear()  # initialize() re-armed the provider
        p._finish_shutdown_when_idle([], pool)
        pool.close.assert_not_called()
        self.assertIs(p._pool, pool)


if __name__ == "__main__":
    unittest.main()
