import argparse
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

scripts_dir = os.path.join(root_dir, "scripts")
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

import tests.conftest
import reembed_embeddings


class TestReembedEmbeddings(unittest.TestCase):
    def test_guard_serial_attempts_manual_and_disable(self):
        cur = MagicMock()
        reembed_embeddings.guard_serial(cur)
        executed_stmts = [call[0][0] for call in cur.execute.call_args_list]
        self.assertIn("ALTER SESSION SET parallel_degree_policy = MANUAL", executed_stmts)
        self.assertIn("ALTER SESSION DISABLE PARALLEL QUERY", executed_stmts)
        self.assertIn("ALTER SESSION DISABLE PARALLEL DML", executed_stmts)

    def test_guard_serial_gracefully_handles_errors(self):
        cur = MagicMock()
        cur.execute.side_effect = Exception("ORA-01031: insufficient privileges")
        # Should not raise exception
        reembed_embeddings.guard_serial(cur)

    def test_assert_no_px_zero_slaves(self):
        cur = MagicMock()
        cur.fetchone.return_value = (0,)
        # Should not raise
        reembed_embeddings.assert_no_px(cur)

    def test_assert_no_px_raises_on_slaves(self):
        cur = MagicMock()
        cur.fetchone.return_value = (4,)
        with self.assertRaises(RuntimeError) as ctx:
            reembed_embeddings.assert_no_px(cur)
        self.assertIn("4 PX slave session(s) appeared", str(ctx.exception))

    def test_partition_bounds_workers_1(self):
        bounds = reembed_embeddings.partition_bounds("TEST_TABLE", 1)
        self.assertEqual(bounds, [None, None])

    @patch("reembed_embeddings.connect")
    def test_partition_bounds_nulls_only(self, mock_connect):
        conn = MagicMock()
        cur = MagicMock()
        mock_connect.return_value = conn
        conn.cursor.return_value = cur
        cur.fetchall.return_value = [("mid1",), ("mid2",)]

        bounds = reembed_embeddings.partition_bounds("TEST_TABLE", 2, nulls_only=True)
        self.assertEqual(len(bounds), 3)
        self.assertEqual(bounds[0], None)
        all_stmts = [c[0][0] for c in cur.execute.call_args_list]
        self.assertTrue(any("embedding IS NULL OR embedding_model IS NULL" in s for s in all_stmts))

    @patch("reembed_embeddings.connect")
    def test_run_worker_nulls_mode_sql(self, mock_connect):
        conn = MagicMock()
        cur = MagicMock()
        mock_connect.return_value = conn
        conn.cursor.return_value = cur

        # Fetch one batch, then empty to terminate
        cur.fetchall.side_effect = [
            [("row1", "content 1"), ("row2", "content 2")],
            [],
        ]
        cur.fetchone.return_value = (0,)  # For assert_no_px
        cur.rowcount = 2

        counters = reembed_embeddings.Counters()
        stop = reembed_embeddings.threading.Event()
        state = {}

        args = argparse.Namespace(
            mode="nulls",
            batch_size=25,
            tolerance=0.01,
            resume=False,
            pga_max_retries=1,
        )

        reembed_embeddings.run_worker(
            idx=0,
            lo=None,
            hi="zzzzzz",
            args=args,
            model="BGE_BASE_EN_V1_5",
            table="HERMES_AGENT_MEMORY",
            counters=counters,
            state=state,
            stop=stop,
        )

        all_stmts = [c[0][0] for c in cur.execute.call_args_list]
        self.assertTrue(any("embedding IS NULL OR m.embedding_model IS NULL" in s for s in all_stmts))

        update_stmts = [s for s in all_stmts if "UPDATE HERMES_AGENT_MEMORY" in s]
        self.assertTrue(len(update_stmts) > 0)
        self.assertIn("m.embedding_model = :model", update_stmts[0])
        self.assertIn("m.embedding_updated_at = SYSTIMESTAMP", update_stmts[0])
        self.assertIn("/*+ NO_PARALLEL(m) */", update_stmts[0])

        snap = counters.snapshot()
        self.assertEqual(snap["written"], 2)
        self.assertEqual(snap["scanned"], 2)
        self.assertEqual(snap["failed"], 0)

    @patch("reembed_embeddings.connect")
    def test_run_worker_stale_mode_sql(self, mock_connect):
        conn = MagicMock()
        cur = MagicMock()
        mock_connect.return_value = conn
        conn.cursor.return_value = cur

        cur.fetchall.side_effect = [
            [("row1", "content 1"), ("row2", "content 2")],
            [],
        ]
        cur.fetchone.return_value = (0,)
        cur.rowcount = 1

        counters = reembed_embeddings.Counters()
        stop = reembed_embeddings.threading.Event()
        state = {}

        args = argparse.Namespace(
            mode="stale",
            batch_size=25,
            tolerance=0.01,
            resume=False,
            pga_max_retries=1,
        )

        reembed_embeddings.run_worker(
            idx=0,
            lo=None,
            hi="zzzzzz",
            args=args,
            model="BGE_BASE_EN_V1_5",
            table="HERMES_AGENT_MEMORY",
            counters=counters,
            state=state,
            stop=stop,
        )

        merge_calls = [c for c in cur.execute.call_args_list if "MERGE INTO" in c[0][0]]
        self.assertEqual(len(merge_calls), 1)
        sql, binds = merge_calls[0][0]
        self.assertIn("t.embedding_model = :model", sql)
        self.assertIn("t.embedding_updated_at = SYSTIMESTAMP", sql)
        # Position 1: :lo -> row1, Position 2: :hi -> row2, Position 3: :model -> model, Position 4: :tol -> tolerance
        self.assertEqual(binds, ["row1", "row2", "BGE_BASE_EN_V1_5", 0.01])

    @patch("reembed_embeddings.connect")
    def test_run_worker_reembed_mode_sql(self, mock_connect):
        conn = MagicMock()
        cur = MagicMock()
        mock_connect.return_value = conn
        conn.cursor.return_value = cur

        cur.fetchall.side_effect = [
            [("row1", "content 1"), ("row2", "content 2")],
            [],
        ]
        cur.fetchone.return_value = (0,)
        cur.rowcount = 2

        counters = reembed_embeddings.Counters()
        stop = reembed_embeddings.threading.Event()
        state = {}

        args = argparse.Namespace(
            mode="reembed",
            batch_size=25,
            tolerance=0.01,
            resume=False,
            pga_max_retries=1,
        )

        reembed_embeddings.run_worker(
            idx=0,
            lo=None,
            hi="zzzzzz",
            args=args,
            model="BGE_BASE_EN_V1_5",
            table="HERMES_AGENT_MEMORY",
            counters=counters,
            state=state,
            stop=stop,
        )

        update_calls = [c for c in cur.execute.call_args_list if "UPDATE HERMES_AGENT_MEMORY" in c[0][0]]
        self.assertEqual(len(update_calls), 1)
        sql, binds = update_calls[0][0]
        self.assertIn("m.embedding_model = :model", sql)
        self.assertEqual(binds, ["BGE_BASE_EN_V1_5", "row1", "row2"])

    def test_assert_no_px_session_scoped_query(self):
        cur = MagicMock()
        cur.fetchone.return_value = (0,)
        reembed_embeddings.assert_no_px(cur)
        executed_stmts = [c[0][0] for c in cur.execute.call_args_list]
        self.assertTrue(any("USERENV" in s and "qcsid" in s for s in executed_stmts))

    @patch("reembed_embeddings.run_worker")
    @patch("reembed_embeddings.partition_bounds")
    def test_run_safety_enforcement_in_nulls_mode(self, mock_bounds, mock_worker):
        mock_bounds.return_value = [None, None]
        args = argparse.Namespace(
            mode="nulls",
            batch_size=100,
            workers=4,
            total_rows=162_400,
            table="HERMES_AGENT_MEMORY",
            onnx_model="BGE_BASE_EN_V1_5",
            checkpoint=None,
            resume=False,
            hi_sentinel="zzzzzz",
            limit=0,
        )
        with patch("reembed_embeddings.load_env"):
            reembed_embeddings.run(args)
        self.assertEqual(args.batch_size, 25)
        self.assertEqual(args.workers, 1)
        self.assertEqual(args.total_rows, 1228)


if __name__ == "__main__":
    unittest.main()
