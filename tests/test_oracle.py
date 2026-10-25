import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import tests.conftest
import __init__ as oracle_plugin


class TestOraclePlugin(unittest.TestCase):
    def test_recall_status_none(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._prefetch_count = 0
        self.assertIsNone(provider.recall_status())

    def test_recall_status_positive(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._prefetch_count = 5
        status = provider.recall_status()
        self.assertIsNotNone(status)
        self.assertEqual(status.provider_label, "Oracle 26ai")
        self.assertEqual(status.count, 5)

    def test_recall_status_negative(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._prefetch_count = -1
        self.assertIsNone(provider.recall_status())

    def test_contains_query_edge_cases(self):
        provider = oracle_plugin.OracleMemoryProvider()

        # Test None
        self.assertEqual(provider._contains_query(None), "{memory}")

        # Test empty string
        self.assertEqual(provider._contains_query(""), "{memory}")

        # Test whitespace only
        self.assertEqual(provider._contains_query("   \n\t  "), "{memory}")

        # Test only punctuation
        self.assertEqual(provider._contains_query("?!.,;:()"), "{memory}")

        # Test only short words
        self.assertEqual(provider._contains_query("a to is"), "{memory}")

        # Test valid query
        self.assertEqual(provider._contains_query("test query"), "{test} ACCUM {query}")

        # Test hyphen
        self.assertEqual(provider._contains_query("top-secret info"), "{top} ACCUM {secret} ACCUM {info}")

        # Test punctuation
        self.assertEqual(provider._contains_query("hello, world! how's it going?"), "{hello} ACCUM {world} ACCUM {how} ACCUM {going}")

        # Test max tokens
        long_query = "one two three four five six seven eight nine ten"
        expected = "{one} ACCUM {two} ACCUM {three} ACCUM {four} ACCUM {five} ACCUM {six} ACCUM {seven} ACCUM {eight}"
        self.assertEqual(provider._contains_query(long_query), expected)

    def test_contains_query_reserved_words(self):
        provider = oracle_plugin.OracleMemoryProvider()

        # Test queries with reserved words: not, and, or, accum, minus, within
        # Note: 'or' has length 2, so it is filtered out by the len(t) > 2 length check
        query = "not and or accum minus within"
        expected = "{not} ACCUM {and} ACCUM {accum} ACCUM {minus} ACCUM {within}"
        self.assertEqual(provider._contains_query(query), expected)

        # Test single reserved words
        self.assertEqual(provider._contains_query("not"), "{not}")
        self.assertEqual(provider._contains_query("and"), "{and}")
        self.assertEqual(provider._contains_query("accum"), "{accum}")
        self.assertEqual(provider._contains_query("minus"), "{minus}")
        self.assertEqual(provider._contains_query("within"), "{within}")
        self.assertEqual(provider._contains_query("near"), "{near}")

        # Test short reserved word 'or' falls back to {memory}
        self.assertEqual(provider._contains_query("or"), "{memory}")

        # Test phrase containing 'or' alongside longer terms
        self.assertEqual(provider._contains_query("gold or silver"), "{gold} ACCUM {silver}")

        # Test uppercase reserved words are wrapped
        self.assertEqual(
            provider._contains_query("NOT AND ACCUM MINUS WITHIN"),
            "{NOT} ACCUM {AND} ACCUM {ACCUM} ACCUM {MINUS} ACCUM {WITHIN}",
        )

    def test_initialize_handles_magicmock_and_non_strings(self):
        provider = oracle_plugin.OracleMemoryProvider()
        # Mock get_secret to return MagicMock as happens when dependencies are mocked
        mock_get_secret = MagicMock()
        with patch.object(oracle_plugin, "get_secret", mock_get_secret):
            # Should not raise TypeError: expected string or bytes-like object, got 'MagicMock'
            provider.initialize(MagicMock(), agent_identity=MagicMock(), agent_context=MagicMock())
            self.assertEqual(provider._table, "hermes_agent_memory")
            self.assertEqual(provider._session_id, "")
            self.assertEqual(provider._agent_id, "hermes")
            self.assertEqual(provider._agent_context, "primary")
            self.assertEqual(provider._init_error, "OCI_DB_PASSWORD or OCI_DB_DSN missing")

    def test_is_available_handles_magicmock(self):
        provider = oracle_plugin.OracleMemoryProvider()
        with patch.object(oracle_plugin, "get_secret", return_value=MagicMock()):
            # MagicMock should not be considered a valid string credential
            self.assertFalse(provider.is_available())

    def test_unavailable_reason_handles_magicmock(self):
        provider = oracle_plugin.OracleMemoryProvider()
        with patch.object(oracle_plugin, "get_secret", return_value=MagicMock()), \
             patch.dict(sys.modules, {"oracledb": MagicMock()}):
            reason = provider.unavailable_reason()
            self.assertIn("missing env", reason)

    def test_unavailable_reason_missing_oracledb(self):
        provider = oracle_plugin.OracleMemoryProvider()
        with patch.dict(sys.modules, {"oracledb": None}):
            reason = provider.unavailable_reason()
            self.assertEqual(reason, "oracledb is not installed in the Hermes venv")

    def test_is_available_no_oracledb(self):
        provider = oracle_plugin.OracleMemoryProvider()
        with patch.dict(sys.modules, {"oracledb": None}):
            self.assertFalse(provider.is_available())

    def test_is_available_missing_secrets(self):
        provider = oracle_plugin.OracleMemoryProvider()
        with patch.dict(sys.modules, {"oracledb": MagicMock()}):
            with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, *args: None):
                self.assertFalse(provider.is_available())

    def test_is_available_some_missing_secrets(self):
        provider = oracle_plugin.OracleMemoryProvider()
        secrets = {
            "OCI_DB_USER": "user",
            "OCI_DB_PASSWORD": None,
            "OCI_DB_DSN": "dsn",
        }
        with patch.dict(sys.modules, {"oracledb": MagicMock()}):
            with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, *args: secrets.get(k)):
                self.assertFalse(provider.is_available())

    def test_is_available_all_present(self):
        provider = oracle_plugin.OracleMemoryProvider()
        secrets = {
            "OCI_DB_USER": "user",
            "OCI_DB_PASSWORD": "password",
            "OCI_DB_DSN": "dsn",
        }
        with patch.dict(sys.modules, {"oracledb": MagicMock()}):
            with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, *args: secrets.get(k)):
                self.assertTrue(provider.is_available())

    def test_embed_openrouter_removed(self):
        provider = oracle_plugin.OracleMemoryProvider()
        self.assertFalse(hasattr(provider, "_embed_openrouter"))

    def test_embed_many_tagged_raises_on_unconfigured_onnx(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._onnx_model = ""
        provider._pool = MagicMock()
        with self.assertRaises(RuntimeError) as ctx:
            provider._embed_many_tagged(["test text"])
        self.assertIn("external fallback is disabled", str(ctx.exception))

    def test_embed_many_tagged_raises_on_uninitialized_pool(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        provider._pool = None
        with self.assertRaises(RuntimeError) as ctx:
            provider._embed_many_tagged(["test text"])
        self.assertIn("session pool is not initialized", str(ctx.exception))

    def test_embed_many_tagged_raises_on_cooldown(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        provider._pool = MagicMock()
        provider._onnx_failed_at = 9999999999.0
        with self.assertRaises(RuntimeError) as ctx:
            provider._embed_many_tagged(["test text"])
        self.assertIn("in cooldown", str(ctx.exception))

    def test_embed_many_tagged_raises_on_onnx_failure(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn
        mock_cur.execute.side_effect = Exception("ORA-29532: Java or ONNX call failed")
        with self.assertRaises(RuntimeError) as ctx:
            provider._embed_many_tagged(["test text"])
        self.assertIn("In-database ONNX embedding failed", str(ctx.exception))
        self.assertTrue(provider._onnx_in_cooldown())

    def test_embed_many_deduplicates_embedding_misses(self):
        import array

        provider = oracle_plugin.OracleMemoryProvider()
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        first = array.array("f", [0.1] * 768)
        second = array.array("f", [0.2] * 768)
        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn
        mock_cur.fetchall.return_value = [(0, first), (1, second)]

        result = provider._embed_many(["duplicate", "duplicate", "unique"])

        self.assertEqual(result, [first, first, second])
        self.assertEqual(mock_cur.execute.call_count, 1)
        binds = mock_cur.execute.call_args[0][1]
        self.assertEqual(binds, ["duplicate", "unique"])

    def test_embed_many_cools_down_after_onnx_failure(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        provider._pool = MagicMock()
        provider._pool.acquire.side_effect = Exception("model unavailable")

        with self.assertRaises(RuntimeError):
            provider._embed_many(["first"])
        with self.assertRaises(RuntimeError):
            provider._embed_many(["second"])

        self.assertEqual(provider._pool.acquire.call_count, 1)
        self.assertTrue(provider._onnx_in_cooldown())

    def test_search_does_not_client_embed_when_onnx_is_configured(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        provider._pool = MagicMock()
        provider._embed = MagicMock(side_effect=AssertionError("unexpected client embedding"))
        provider._execute_hybrid_search = MagicMock(return_value=[])

        self.assertEqual(provider._search("known query"), [])
        provider._embed.assert_not_called()
        provider._execute_hybrid_search.assert_called_once()

    def test_single_result_skips_rerank(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = ""
        provider._fetch_rows = MagicMock(
            return_value=[{"memory_id": "m1", "score": 1.0}]
        )
        provider._rerank = MagicMock(side_effect=AssertionError("unexpected rerank"))
        row = {"memory_id": "m1", "score": 1.0}

        result = provider._execute_hybrid_search(
            MagicMock(), "q", [0.1] * 768, "q", 1, 1, "", "any"
        )

        self.assertEqual(result, [row])
        provider._rerank.assert_not_called()

    def test_execute_hybrid_search_oracle_text_error_fallback(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        mock_cur = MagicMock()
        mock_cur.arraysize = 64
        mock_cur.prefetchrows = 64

        # Simulate Oracle Text error on hybrid query, but success on pure vector fallback
        hybrid_err = Exception("ORA-29902: error in executing ODCIIndexStart() routine (DRG-50901: text query parser syntax error)")
        vector_fallback_rows = [
            ("m1", "s1", "a1", "user", "vector fallback content", "memory", 0.0, 0.2)
        ]
        mock_cur.execute.side_effect = [hybrid_err, None]
        mock_cur.fetchall.return_value = vector_fallback_rows

        provider._onnx_mark_failed = MagicMock()

        results = provider._execute_hybrid_search(
            mock_cur,
            query="test query with reserved words",
            vec=None,
            contains_q="{test} ACCUM {query}",
            top_k=5,
            fetch_k=10,
            tgt_sql="",
            tgt="any",
            rerank=False,
        )

        # Oracle Text errors must NOT trigger ONNX cooldown
        provider._onnx_mark_failed.assert_not_called()
        self.assertFalse(provider._onnx_in_cooldown())

        # Must return the vector search fallback results
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["memory_id"], "m1")
        self.assertEqual(results[0]["content"], "vector fallback content")
        self.assertAlmostEqual(results[0]["score"], 0.7 * (1.0 - 0.2), places=4)

    def test_execute_hybrid_search_oracle_text_error_variations_do_not_mark_onnx_failed(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        text_error_messages = [
            "DRG-50901: text query parser syntax error",
            "ORA-29902: error in executing ODCIIndexStart() routine",
            "ORA-30600: text query parser error",
        ]

        for err_msg in text_error_messages:
            mock_cur = MagicMock()
            mock_cur.arraysize = 64
            mock_cur.prefetchrows = 64
            mock_cur.execute.side_effect = [
                Exception(err_msg),
                None,
            ]
            mock_cur.fetchall.return_value = [
                ("m2", "s1", "a1", "user", "recovered", "memory", 0.0, 0.1)
            ]

            provider._onnx_mark_failed = MagicMock()

            results = provider._execute_hybrid_search(
                mock_cur,
                query="query",
                vec=None,
                contains_q="{query}",
                top_k=5,
                fetch_k=10,
                tgt_sql="",
                tgt="any",
                rerank=False,
            )

            provider._onnx_mark_failed.assert_not_called()
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["memory_id"], "m2")

    def test_execute_hybrid_search_non_text_error_marks_onnx_failed(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        mock_cur = MagicMock()
        mock_cur.execute.side_effect = Exception("ORA-40284: model does not exist")

        provider._onnx_mark_failed = MagicMock()

        with self.assertRaises(RuntimeError) as ctx:
            provider._execute_hybrid_search(
                mock_cur,
                query="test query",
                vec=None,
                contains_q="{test}",
                top_k=5,
                fetch_k=10,
                tgt_sql="",
                tgt="any",
            )

        self.assertIn("In-database ONNX hybrid search failed", str(ctx.exception))
        provider._onnx_mark_failed.assert_called_once()

    def test_fetch_rows_tunes_oracle_fetch_buffers(self):
        provider = oracle_plugin.OracleMemoryProvider()
        cursor = MagicMock()
        cursor.arraysize = 1
        cursor.fetchall.return_value = []

        with patch.dict(sys.modules, {"oracledb": MagicMock(DB_TYPE_CLOB=object(), DB_TYPE_LONG=object())}):
            provider._fetch_rows(cursor, "SELECT 1", {"k": 8})

        self.assertEqual(cursor.arraysize, oracle_plugin._FETCH_ARRAYSIZE)
        self.assertEqual(cursor.prefetchrows, oracle_plugin._FETCH_ARRAYSIZE)

    def test_fetch_rows_converts_clob_lob_to_str(self):
        provider = oracle_plugin.OracleMemoryProvider()

        class FakeLob:
            def read(self):
                return "lob content here"

        cursor = MagicMock()
        cursor.arraysize = 1
        # row: memory_id, session_id, agent_id, role, content, target, txt_score, distance
        cursor.fetchall.return_value = [
            ("m1", "s1", "a1", "user", FakeLob(), "memory", 1.0, 0.25),
            ("m2", "s2", "a2", "assistant", "already str", "user", 2.0, None),
            ("m3", "s3", "a3", "user", None, "memory", 0.0, 0.9),
        ]

        rows = provider._fetch_rows(cursor, "SELECT 1", {"k": 8})

        self.assertEqual(rows[0]["content"], "lob content here")
        self.assertIsInstance(rows[0]["content"], str)
        self.assertEqual(rows[1]["content"], "already str")
        self.assertIsNone(rows[2]["content"])
        self.assertAlmostEqual(rows[0]["score"], 0.7 * (1.0 - 0.25) + 0.3 * 0.1, places=4)

    def test_sync_turn_queues_without_waiting_for_previous_write(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        with patch.object(provider, "_queue_background_insert") as queue:
            provider.sync_turn("user message", "assistant reply", session_id="s1")

        queue.assert_called_once()
        self.assertEqual(queue.call_args.kwargs["session_id"], "s1")

    def test_session_end_queues_background_write(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        messages = [
            {"role": "user", "content": "A durable session detail worth retaining."},
        ]
        with patch.object(provider, "_queue_background_insert") as queue:
            provider.on_session_end(messages)

        queue.assert_called_once()
        self.assertEqual(queue.call_args.kwargs["role"], "session")

    def test_onnx_search_raises_on_failure_no_fallback(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        mock_cur.fetchall.side_effect = Exception("ORA-29532: Java or ONNX call failed")

        with self.assertRaises(RuntimeError) as ctx:
            provider._search("database query", mode="vector")
        self.assertIn("In-database ONNX vector search failed", str(ctx.exception))
        self.assertIn("external fallback is disabled", str(ctx.exception))
        self.assertTrue(provider._onnx_in_cooldown())

    def test_onnx_insert_raises_on_failure_no_fallback(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        mock_cur.execute.side_effect = Exception("ORA-40284: model does not exist")

        with self.assertRaises(RuntimeError) as ctx:
            provider._insert(
                memory_id="test_id",
                session_id="s1",
                role="user",
                content="test content",
                target="memory",
            )
        self.assertIn("In-database ONNX insert failed", str(ctx.exception))
        self.assertIn("external fallback is disabled", str(ctx.exception))
        self.assertTrue(provider._onnx_in_cooldown())

    def test_onnx_update_raises_on_failure_no_fallback(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        mock_cur.execute.side_effect = Exception("ORA-40284: model does not exist")

        with patch.object(oracle_plugin, "tool_error", side_effect=lambda msg: f"Error: {msg}"):
            res = provider.handle_tool_call("oracle_update", {"memory_id": "m1", "content": "updated content"})
            self.assertIn("In-database ONNX update failed", res)
            self.assertIn("external fallback is disabled", res)
            self.assertTrue(provider._onnx_in_cooldown())

    def test_onnx_update_does_not_client_embed_before_sql_attempt(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        provider._pool = MagicMock()
        provider._pool.acquire.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.rowcount = 1
        provider._embed = MagicMock(side_effect=AssertionError("unexpected client embedding"))

        result = provider.handle_tool_call(
            "oracle_update", {"memory_id": "m1", "content": "updated content"}
        )

        self.assertIn('"result": "updated"', result)
        provider._embed.assert_not_called()

    def test_embed_many_batches_onnx_in_single_query(self):
        import array

        provider = oracle_plugin.OracleMemoryProvider()
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        provider._pool = MagicMock()

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        provider._pool.acquire.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        vec = array.array("f", [0.1] * 768)
        mock_cur.fetchall.return_value = [(0, vec), (1, vec), (2, vec)]

        result = provider._embed_many(["alpha", "beta", "gamma"])

        self.assertEqual(len(result), 3)
        self.assertTrue(all(v is not None for v in result))
        # A single UNION ALL query, not one round-trip per text (N+1).
        self.assertEqual(mock_cur.execute.call_count, 1)
        self.assertIn("UNION ALL", mock_cur.execute.call_args[0][0])
        self.assertEqual(mock_cur.fetchall.call_count, 1)

    # -- embedding provenance (embedding_model / embedding_updated_at) ----------

    def _mocked_conn(self, rowcount=1):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.rowcount = rowcount
        return mock_conn, mock_cur

    def test_insert_stamps_onnx_provenance(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        provider._insert(
            memory_id="m1", session_id="s1", role="fact",
            content="provenance content", target="memory",
        )
        sql, binds = mock_cur.execute.call_args[0]
        self.assertIn("embedding_model", sql)
        self.assertIn("embedding_updated_at", sql)
        self.assertIn("BGE_BASE_EN_V1_5", binds)

    def test_insert_many_stamps_onnx_provenance(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        provider._insert_many([
            {"memory_id": "m1", "session_id": "s1", "role": "chunk", "content": "a", "target": "memory"},
            {"memory_id": "m2", "session_id": "s1", "role": "chunk", "content": "b", "target": "memory"},
        ])
        sql, binds_list = mock_cur.executemany.call_args[0]
        self.assertIn("embedding_model", sql)
        for binds in binds_list:
            # model tag sits 5th-from-end: ..., model, turn_index, chunk_index,
            # turn_id, conv_ref
            self.assertEqual(binds[-5], "BGE_BASE_EN_V1_5")

    def test_embed_backfill_update_stamps_provenance(self):
        import array
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        provider._pool = MagicMock()
        fake_vec = array.array("f", [0.1] * 768)
        provider._embed_many_tagged = MagicMock(
            return_value=[(fake_vec, "BGE_BASE_EN_V1_5")]
        )
        mock_conn, mock_cur = self._mocked_conn()
        provider._pool.acquire.return_value = mock_conn
        provider._embed_pending.append(("m1", "queued content"))

        provider._embed_worker_loop()

        sql, binds_list = mock_cur.executemany.call_args[0]
        self.assertIn("embedding_model", sql)
        self.assertEqual(binds_list[0][1], "BGE_BASE_EN_V1_5")

    def test_embed_many_tagged_reports_backend(self):
        import array
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn
        fake_vec = array.array("f", [0.1] * 768)
        mock_cur.fetchall.return_value = [(0, fake_vec)]

        result = provider._embed_many_tagged(["hello", "hello"])
        self.assertEqual([v for v, _m in result], [fake_vec, fake_vec])
        self.assertEqual([m for _v, m in result], ["BGE_BASE_EN_V1_5"] * 2)
        # the untagged view stays compatible with search-only callers
        mock_cur.fetchall.return_value = [(0, fake_vec)]
        self.assertEqual(provider._embed_many(["hello"]), [fake_vec])

    def test_embed_cache_preserves_provenance(self):
        import array
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn
        fake_vec = array.array("f", [0.1] * 768)
        mock_cur.fetchall.return_value = [(0, fake_vec)]

        res1 = provider._embed_many_tagged(["same text"])
        res2 = provider._embed_many_tagged(["same text"])
        self.assertEqual(mock_cur.execute.call_count, 1)  # second served from cache
        self.assertEqual(res2[0][1], "BGE_BASE_EN_V1_5")

    def test_onnx_update_stamps_provenance(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn
        provider._queue_embed_backfill = MagicMock()

        result = provider.handle_tool_call(
            "oracle_update", {"memory_id": "m1", "content": "updated content"}
        )
        self.assertIn('"result": "updated"', result)
        sql, binds = mock_cur.execute.call_args[0]
        self.assertIn("embedding_model", sql)
        # binds are positional by order of appearance: model is :4, memory_id :5
        self.assertEqual(binds[3], "BGE_BASE_EN_V1_5")
        self.assertEqual(binds[4], "m1")

    # -- Oracle-first behavior and _low pool tests --------------------------------

    def test_version(self):
        self.assertEqual(oracle_plugin.__version__, "1.0.9.3")

    def test_system_prompt_block_oracle_first_rules(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        provider._agent_id = "test-agent"
        block = provider.system_prompt_block()
        self.assertIn("# Oracle 26ai Memory (Primary Store)", block)
        self.assertIn("1. ORACLE-FIRST SEARCH", block)
        self.assertIn("2. ORACLE-FIRST FACT SINK", block)
        self.assertIn("3. MEMORY_ID LIFECYCLE", block)
        self.assertIn("4. STORAGE TIERS", block)
        self.assertIn("5. ORACLE-ONLY STORE", block)
        self.assertIn("6. ORACLE UNAVAILABILITY", block)
        self.assertIn("7. INTERNAL WRITE-AHEAD", block)
        self.assertIn("8. SESSION SCOPING", block)
        self.assertIn("9. VALIDATION BEFORE WRITE", block)

        provider_down = oracle_plugin.OracleMemoryProvider()
        provider_down._pool = None
        down_block = provider_down.system_prompt_block()
        self.assertIn("No memory store is available. Local memory is prohibited.", down_block)

    def test_tool_schemas_oracle_first(self):
        schemas = {s["name"]: s for s in oracle_plugin.TOOL_SCHEMAS}
        self.assertIn("oracle_search", schemas)
        self.assertIn("oracle_add", schemas)
        # Search must instruct consulting first before local memory
        self.assertIn("ALWAYS consult oracle_search FIRST", schemas["oracle_search"]["description"])
        # Add must instruct primary sink and mention unlimited storage / not using local memory
        self.assertIn("Primary sink for persistent facts", schemas["oracle_add"]["description"])
        self.assertIn("facts", schemas["oracle_add"]["parameters"]["properties"])

    def test_oracle_add_single_fact(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        provider._session_id = "sess-1"
        provider._insert = MagicMock()

        res = provider.handle_tool_call("oracle_add", {"content": "User prefers python3"})
        data = json.loads(res)
        self.assertEqual(data["result"], "Fact stored.")
        self.assertTrue(data["memory_id"].startswith("fact_"))
        provider._insert.assert_called_once()
        kwargs = provider._insert.call_args[1]
        self.assertEqual(kwargs["content"], "User prefers python3")
        self.assertEqual(kwargs["target"], "memory")

    def test_oracle_add_batch_facts_via_facts_parameter(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        provider._session_id = "sess-1"
        provider._insert = MagicMock()

        res = provider.handle_tool_call("oracle_add", {
            "facts": ["Fact 1: server is Linux", "Fact 2: Python 3.12 active"],
            "target": "memory",
        })
        data = json.loads(res)
        self.assertIn("Batch facts stored (2)", data["result"])
        self.assertEqual(len(data["memory_ids"]), 2)
        self.assertEqual(data["count"], 2)
        self.assertEqual(provider._insert.call_count, 2)

    def test_oracle_add_batch_facts_via_content_list(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        provider._session_id = "sess-1"
        provider._insert = MagicMock()

        res = provider.handle_tool_call("oracle_add", {
            "content": ["Fact A", "Fact B", "Fact C"],
            "target": "user",
        })
        data = json.loads(res)
        self.assertEqual(data["count"], 3)
        self.assertEqual(provider._insert.call_count, 3)

    def test_oracle_add_stringified_json_array(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        provider._session_id = "sess-1"
        provider._insert = MagicMock()

        res = provider.handle_tool_call("oracle_add", {
            "facts": '["Fact X: parsed from JSON string", "Fact Y: JSON string"]',
            "target": "memory",
        })
        data = json.loads(res)
        self.assertEqual(data["count"], 2)
        self.assertEqual(provider._insert.call_count, 2)

    def test_oracle_add_content_stringified_json_array(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        provider._session_id = "sess-1"
        provider._insert = MagicMock()

        res = provider.handle_tool_call("oracle_add", {
            "content": '["Fact Alpha", "Fact Beta"]',
        })
        data = json.loads(res)
        self.assertEqual(data["count"], 2)
        self.assertEqual(provider._insert.call_count, 2)

    def test_oracle_add_dict_facts(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        provider._session_id = "sess-1"
        provider._insert = MagicMock()

        res = provider.handle_tool_call("oracle_add", {
            "facts": [{"fact": "Dict fact 1"}, {"text": "Dict fact 2"}],
            "target": "invalid_target",
        })
        data = json.loads(res)
        self.assertEqual(data["count"], 2)
        self.assertEqual(provider._insert.call_count, 2)
        # Invalid target must normalize to "memory"
        self.assertEqual(provider._insert.call_args_list[0][1]["target"], "memory")
        self.assertEqual(provider._insert.call_args_list[0][1]["content"], "Dict fact 1")
        self.assertEqual(provider._insert.call_args_list[1][1]["content"], "Dict fact 2")

    def test_oracle_add_missing_content(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        with patch.object(oracle_plugin, "tool_error", side_effect=lambda msg: f"Error: {msg}"):
            res = provider.handle_tool_call("oracle_add", {})
            self.assertIn("Missing required parameter", res)

    def test_oracle_add_whitespace_and_empty_facts(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        with patch.object(oracle_plugin, "tool_error", side_effect=lambda msg: f"Error: {msg}"):
            res = provider.handle_tool_call("oracle_add", {"facts": ["  ", ""]})
            self.assertIn("Missing required parameter", res)
            res2 = provider.handle_tool_call("oracle_add", {"facts": []})
            self.assertIn("Missing required parameter", res2)

    def test_oracle_add_facts_as_string(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        provider._session_id = "sess-1"
        provider._insert = MagicMock()

        res = provider.handle_tool_call("oracle_add", {"facts": "User prefers Linux"})
        data = json.loads(res)
        self.assertEqual(data["result"], "Fact stored.")
        self.assertEqual(provider._insert.call_count, 1)

    def test_tool_schemas_update_delete_anti_conflation(self):
        schemas = {s["name"]: s for s in oracle_plugin.TOOL_SCHEMAS}
        self.assertIn("oracle_update", schemas)
        self.assertIn("oracle_delete", schemas)
        self.assertIn("Never use the local memory tool", schemas["oracle_update"]["description"])
        self.assertIn("chk_* or fact_*", schemas["oracle_update"]["description"])
        self.assertIn("Never use the local memory tool", schemas["oracle_delete"]["description"])
        self.assertIn("chk_* or fact_*", schemas["oracle_delete"]["description"])

    def test_dsn_high_to_low_normalization_and_pool_limits(self):
        secrets = {
            "OCI_DB_USER": "TESTUSER",
            "OCI_DB_PASSWORD": "TESTPASSWORD",
            "OCI_DB_DSN": "db_high",
        }
        with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, d=None: secrets.get(k, d)):
            with patch("oracledb.SessionPool") as mock_session_pool:
                provider = oracle_plugin.OracleMemoryProvider()
                provider._verify_schema = MagicMock()
                provider._ensure_schema = MagicMock()
                provider._ensure_write_worker = MagicMock()
                provider.initialize("test-session")
                self.assertEqual(provider._dsn, "db_low")
                self.assertTrue(mock_session_pool.called)
                pool_kwargs = mock_session_pool.call_args[1]
                self.assertEqual(pool_kwargs["min"], 2)
                self.assertEqual(pool_kwargs["max"], 6)
                self.assertEqual(pool_kwargs["call_timeout"], 13000)
                self.assertEqual(pool_kwargs["dsn"], "db_low")

    def test_dsn_high_descriptor_to_low_normalization(self):
        secrets = {
            "OCI_DB_USER": "TESTUSER",
            "OCI_DB_PASSWORD": "TESTPASSWORD",
            "OCI_DB_DSN": "(DESCRIPTION=(CONNECT_DATA=(SERVICE_NAME=myadb_high.adb.oraclecloud.com)))",
        }
        with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, d=None: secrets.get(k, d)):
            with patch("oracledb.SessionPool") as mock_session_pool:
                provider = oracle_plugin.OracleMemoryProvider()
                provider._verify_schema = MagicMock()
                provider._ensure_schema = MagicMock()
                provider._ensure_write_worker = MagicMock()
                provider.initialize("test-session")
                self.assertIn("myadb_low.adb.oraclecloud.com", provider._dsn)
                self.assertNotIn("myadb_high", provider._dsn)

    def test_dsn_uppercase_high_normalization(self):
        secrets = {
            "OCI_DB_USER": "TESTUSER",
            "OCI_DB_PASSWORD": "TESTPASSWORD",
            "OCI_DB_DSN": "(DESCRIPTION=(CONNECT_DATA=(SERVICE_NAME=MYADB_HIGH.adb.oraclecloud.com)))",
        }
        with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, d=None: secrets.get(k, d)):
            with patch("oracledb.SessionPool") as mock_session_pool:
                provider = oracle_plugin.OracleMemoryProvider()
                provider._verify_schema = MagicMock()
                provider._ensure_schema = MagicMock()
                provider._ensure_write_worker = MagicMock()
                provider.initialize("test-session")
                self.assertIn("MYADB_low.adb.oraclecloud.com", provider._dsn)
                self.assertNotIn("MYADB_HIGH", provider._dsn)

    def test_live_queries_contain_no_parallel_hints(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        txt_sql, _ = provider._build_text_search_sql("query", 8, "", "any")
        self.assertIn("/*+ NO_PARALLEL */", txt_sql)

        vec_sql_onnx, _ = provider._build_vector_search_sql("query", None, 8, "", "any", use_onnx=True)
        self.assertIn("/*+ NO_PARALLEL */", vec_sql_onnx)

        vec_sql_ext, _ = provider._build_vector_search_sql("query", [0.1] * 768, 8, "", "any", use_onnx=False)
        self.assertIn("/*+ NO_PARALLEL */", vec_sql_ext)

        hybrid_sql_onnx, _ = provider._build_hybrid_search_sql("query", None, "query", 8, "", "any", use_onnx=True)
        self.assertIn("/*+ NO_PARALLEL */", hybrid_sql_onnx)

        hybrid_sql_ext, _ = provider._build_hybrid_search_sql("query", [0.1] * 768, "query", 8, "", "any", use_onnx=False)
        self.assertIn("/*+ NO_PARALLEL */", hybrid_sql_ext)

    def test_dml_queries_contain_no_parallel_hints(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        # Test insert
        provider._insert(
            memory_id="m1", session_id="s1", role="fact",
            content="fact text", target="memory",
        )
        sql, _ = mock_cur.execute.call_args[0]
        self.assertIn("/*+ NO_PARALLEL */", sql)

        # Test delete
        provider.handle_tool_call("oracle_delete", {"memory_id": "m1"})
        sql, _ = mock_cur.execute.call_args[0]
        self.assertIn("/*+ NO_PARALLEL */", sql)

        # Test update
        provider.handle_tool_call("oracle_update", {"memory_id": "m1", "content": "updated"})
        sql, _ = mock_cur.execute.call_args[0]
        self.assertIn("/*+ NO_PARALLEL */", sql)

    def test_session_callback_enforces_serial(self):
        provider = oracle_plugin.OracleMemoryProvider()
        secrets = {
            "OCI_DB_USER": "testuser",
            "OCI_DB_PASSWORD": "supersecretpassword123",
            "OCI_DB_DSN": "testdsn_low",
            "HERMES_ORACLE_TABLE": "TEST_TABLE",
        }
        with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, d=None: secrets.get(k, d)), \
             patch("oracledb.SessionPool") as mock_pool_cls:
            mock_pool = MagicMock()
            mock_pool_cls.return_value = mock_pool
            mock_conn, mock_cur = self._mocked_conn()
            mock_pool.acquire.return_value = mock_conn

            provider.initialize(session_id="test_sess")
            self.assertEqual(mock_pool_cls.call_count, 1)
            pool_kwargs = mock_pool_cls.call_args[1]
            self.assertIn("session_callback", pool_kwargs)
            cb = pool_kwargs["session_callback"]

            test_conn = MagicMock()
            test_cur = MagicMock()
            test_conn.cursor.return_value.__enter__.return_value = test_cur
            cb(test_conn)

            executed = [call[0][0] for call in test_cur.execute.call_args_list]
            self.assertIn("ALTER SESSION SET parallel_degree_policy = MANUAL", executed)
            self.assertIn("ALTER SESSION DISABLE PARALLEL QUERY", executed)
            self.assertIn("ALTER SESSION DISABLE PARALLEL DML", executed)

    def test_password_redacted_on_init_failure(self):
        provider = oracle_plugin.OracleMemoryProvider()
        secret_pwd = "my_super_secret_db_pass_987"
        secrets = {
            "OCI_DB_USER": "testuser",
            "OCI_DB_PASSWORD": secret_pwd,
            "OCI_DB_DSN": "testdsn_low",
        }
        with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, d=None: secrets.get(k, d)), \
             patch("oracledb.SessionPool", side_effect=Exception(f"Connection failed to db with password {secret_pwd} ORA-01017")):
            provider.initialize(session_id="test_sess")
            self.assertNotIn(secret_pwd, provider._init_error)
            self.assertIn("******", provider._init_error)

    def test_additional_queries_contain_no_parallel_hints(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        # Test _insert_many ONNX
        provider._insert_many([{"memory_id": "m1", "content": "text 1"}])
        sql, _ = mock_cur.executemany.call_args[0]
        self.assertIn("/*+ NO_PARALLEL */", sql)

        # Test _insert_many fallback
        provider._onnx_model = None
        provider._insert_many([{"memory_id": "m2", "content": "text 2", "embedding": [0.1] * 768}])
        sql, _ = mock_cur.executemany.call_args[0]
        self.assertIn("/*+ NO_PARALLEL */", sql)

        # Test _chunk_text
        provider._chunk_text("Some text to chunk into pieces", 100, 10)
        chunk_sql = mock_cur.execute.call_args[0][0]
        self.assertIn("/*+ NO_PARALLEL */", chunk_sql)

    def test_schema_policy_require_existing(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn
        provider._verify_schema = MagicMock()

        with patch.dict(os.environ, {"HERMES_ORACLE_SCHEMA_POLICY": "REQUIRE_EXISTING"}):
            provider._ensure_schema()
            self.assertEqual(provider._verify_schema.call_count, 1)
            # execute should not be called for DDL statements
            self.assertEqual(mock_cur.execute.call_count, 0)

    def test_cascading_thread_purge_session_id(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        mock_conn, mock_cur = self._mocked_conn(rowcount=5)
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        # Missing both memory_id and session_id
        with patch.object(oracle_plugin, "tool_error", side_effect=lambda msg: f"Error: {msg}"):
            res_err = provider.handle_tool_call("oracle_delete", {})
            self.assertIn("memory_id or session_id required", res_err)

        # Purge by session_id
        res_ok = json.loads(provider.handle_tool_call("oracle_delete", {"session_id": "thread-456"}))
        self.assertEqual(res_ok["result"], "thread_deleted")
        self.assertEqual(res_ok["session_id"], "thread-456")
        self.assertEqual(res_ok["deleted_count"], 5)
        sql, binds = mock_cur.execute.call_args[0]
        self.assertIn("DELETE /*+ NO_PARALLEL */ FROM HERMES_AGENT_MEMORY WHERE session_id = :1", sql)
        self.assertEqual(binds, ("thread-456",))

    def test_oracle_search_scoped_filters(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        txt_sql, txt_binds = provider._build_text_search_sql("query", 8, "", "any", session_id="s1", agent_id="a1")
        self.assertIn("AND session_id = :sid", txt_sql)
        self.assertIn("AND agent_id = :aid", txt_sql)
        self.assertIn(provider._JUNK.strip(), txt_sql)
        self.assertEqual(txt_binds["sid"], "s1")
        self.assertEqual(txt_binds["aid"], "a1")

        vec_sql, vec_binds = provider._build_vector_search_sql("query", None, 8, "", "any", use_onnx=True, session_id="s1", agent_id="a1")
        self.assertIn("AND session_id = :sid", vec_sql)
        self.assertIn("AND agent_id = :aid", vec_sql)
        self.assertIn(provider._JUNK.strip(), vec_sql)
        self.assertEqual(vec_binds["sid"], "s1")
        self.assertEqual(vec_binds["aid"], "a1")

        hybrid_sql, hybrid_binds = provider._build_hybrid_search_sql("query", None, "query", 8, "", "any", use_onnx=True, session_id="s1", agent_id="a1")
        self.assertIn("AND session_id = :sid", hybrid_sql)
        self.assertIn("AND agent_id = :aid", hybrid_sql)
        self.assertIn(provider._JUNK.strip(), hybrid_sql)
        self.assertEqual(hybrid_binds["sid"], "s1")
        self.assertEqual(hybrid_binds["aid"], "a1")

        # Test handle_tool_call passing scoping filters to _search
        provider._search = MagicMock(return_value=[{"memory_id": "m1", "content": "res", "score": 0.9}])
        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn
        res = provider.handle_tool_call("oracle_search", {"query": "test", "session_id": "s1", "agent_id": "a1"})
        self.assertIn("m1", res)
        provider._search.assert_called_with("test", top_k=8, mode="hybrid", target="any", session_id="s1", agent_id="a1")

    def test_handle_tool_call_non_dict_args(self):
        provider = oracle_plugin.OracleMemoryProvider()
        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn
        # Passing None as args should be handled defensively and not raise TypeError
        with patch.object(oracle_plugin, "tool_error", side_effect=lambda msg: f"Error: {msg}"):
            res = provider.handle_tool_call("oracle_delete", None)
            self.assertIn("memory_id or session_id required", res)

    def test_onnx_retry_cooldown_is_60_seconds(self):
        self.assertEqual(oracle_plugin._ONNX_RETRY_AFTER_SECS, 60.0)

    def test_safe_embed_text_truncation(self):
        self.assertEqual(oracle_plugin._safe_embed_text(None), "")
        self.assertEqual(oracle_plugin._safe_embed_text(""), "")
        self.assertEqual(oracle_plugin._safe_embed_text("short text"), "short text")
        long_text = "x" * 2500
        truncated = oracle_plugin._safe_embed_text(long_text)
        self.assertEqual(len(truncated), 1200)
        self.assertEqual(oracle_plugin.OracleMemoryProvider._safe_embed_text(long_text), truncated)

    def test_execute_hybrid_search_onnx_cooldown_falls_back_to_text_search(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        provider._onnx_failed_at = 9999999999.0  # in cooldown

        mock_cur = MagicMock()
        mock_cur.arraysize = 64
        mock_cur.prefetchrows = 64
        mock_cur.fetchall.return_value = [
            ("m_cool", "s1", "a1", "user", "cooldown text fallback content", "memory", 5.0, None)
        ]

        results = provider._execute_hybrid_search(
            mock_cur,
            query="test query in cooldown",
            vec=None,
            contains_q="{test} ACCUM {query}",
            top_k=5,
            fetch_k=10,
            tgt_sql="",
            tgt="any",
            rerank=False,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["memory_id"], "m_cool")
        self.assertEqual(results[0]["content"], "cooldown text fallback content")
        sql, binds = mock_cur.execute.call_args[0]
        self.assertIn("CONTAINS(content, :q, 1) > 0", sql)

    def test_ora_54401_insert_falls_back_to_null_embedding_without_cooldown(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        shape_err = Exception("ORA-54401: ONNX runtime error: Non-zero status code returned while running Add node. 512 by 755")
        mock_cur.execute.side_effect = [shape_err, None]

        provider._insert(
            memory_id="m_shape",
            session_id="s1",
            role="user",
            content="very long prompt " * 100,
            target="memory",
        )

        self.assertFalse(provider._onnx_in_cooldown())
        self.assertEqual(mock_cur.execute.call_count, 2)
        fallback_sql, fallback_binds = mock_cur.execute.call_args_list[1][0]
        self.assertIn("NULL", fallback_sql)
        self.assertNotIn("VECTOR_EMBEDDING", fallback_sql)
        self.assertEqual(fallback_binds[0], "m_shape")

    def test_ora_54401_insert_many_falls_back_to_null_embedding(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        shape_err = Exception("ORA-54401: ONNX runtime error: input exceeds 512 tokens")
        mock_cur.executemany.side_effect = [shape_err, None]

        rows = [
            {"memory_id": "m1", "session_id": "s1", "role": "user", "content": "row1 " * 80},
            {"memory_id": "m2", "session_id": "s1", "role": "user", "content": "row2 " * 80},
        ]
        provider._insert_many(rows)

        self.assertFalse(provider._onnx_in_cooldown())
        self.assertEqual(mock_cur.executemany.call_count, 2)
        fallback_sql, fallback_binds_list = mock_cur.executemany.call_args_list[1][0]
        self.assertIn("NULL", fallback_sql)
        self.assertEqual(len(fallback_binds_list), 2)
        self.assertEqual(fallback_binds_list[0][0], "m1")
        self.assertEqual(fallback_binds_list[1][0], "m2")

    def test_ora_54401_in_hybrid_search_does_not_mark_onnx_failed(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        mock_cur = MagicMock()
        mock_cur.execute.side_effect = Exception("ORA-54401: ONNX runtime error: 512 by 755")
        provider._onnx_mark_failed = MagicMock()

        with self.assertRaises(RuntimeError):
            provider._execute_hybrid_search(
                mock_cur,
                query="overflow query",
                vec=None,
                contains_q="{overflow}",
                top_k=5,
                fetch_k=10,
                tgt_sql="",
                tgt="any",
            )

        provider._onnx_mark_failed.assert_not_called()
        self.assertFalse(provider._onnx_in_cooldown())

    def test_ora_54401_in_vector_search_does_not_mark_onnx_failed(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        mock_cur.execute.side_effect = Exception("ORA-54401: ONNX runtime error: 512 by 755")
        provider._onnx_mark_failed = MagicMock()

        with self.assertRaises(RuntimeError):
            provider._search("overflow vector query", mode="vector", top_k=5)

        provider._onnx_mark_failed.assert_not_called()
        self.assertFalse(provider._onnx_in_cooldown())

    def test_vector_search_in_cooldown_raises_runtime_error(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        provider._onnx_failed_at = 9999999999.0  # in cooldown

        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        with self.assertRaises(RuntimeError) as ctx:
            provider._search("test query", mode="vector", top_k=5)
        self.assertIn("in cooldown", str(ctx.exception))

    def test_search_timeout_constants(self):
        self.assertEqual(oracle_plugin._SEARCH_TIMEOUT_SECS, 13.0)
        self.assertEqual(oracle_plugin._SEARCH_TIMEOUT_MS, 13000)

    def test_oracle_search_timeout_clean_fallback(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        mock_conn, mock_cur = self._mocked_conn()
        provider._pool.acquire.return_value = mock_conn

        # Simulate timeout in _search
        provider._search = MagicMock(side_effect=TimeoutError("oracle_search query timed out after 13s: DPI-1067: call timeout of 13000 ms exceeded"))
        res = provider.handle_tool_call("oracle_search", {"query": "hanging query", "top_k": 8})
        payload = json.loads(res)
        self.assertEqual(payload["count"], 0)
        self.assertTrue(payload["timed_out"])
        self.assertIn("timed out after 13s", payload["result"])

    def test_oracle_search_db_timeout_error_string_clean_fallback(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        mock_conn, mock_cur = self._mocked_conn()
        provider._pool.acquire.return_value = mock_conn

        # Simulate DPI-1067 / ORA-01013 exception
        provider._search = MagicMock(side_effect=Exception("ORA-01013: user requested cancel of current operation"))
        res = provider.handle_tool_call("oracle_search", {"query": "hanging query", "top_k": 8})
        payload = json.loads(res)
        self.assertEqual(payload["count"], 0)
        self.assertTrue(payload["timed_out"])
        self.assertIn("timed out after 13s", payload["result"])

    def test_timeout_in_hybrid_search_does_not_mark_onnx_failed(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        mock_cur.execute.side_effect = Exception("DPI-1067: call timeout of 13000 ms exceeded")
        provider._onnx_mark_failed = MagicMock()

        with self.assertRaises(TimeoutError):
            provider._execute_hybrid_search(
                mock_cur,
                query="slow query",
                vec=None,
                contains_q="{slow}",
                top_k=5,
                fetch_k=10,
                tgt_sql="",
                tgt="any",
            )

        provider._onnx_mark_failed.assert_not_called()
        self.assertFalse(provider._onnx_in_cooldown())

    def test_timeout_in_vector_search_does_not_mark_onnx_failed(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        mock_conn, mock_cur = self._mocked_conn()
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        mock_cur.execute.side_effect = Exception("DPI-1067: call timeout of 13000 ms exceeded")
        provider._onnx_mark_failed = MagicMock()

        with self.assertRaises(TimeoutError):
            provider._search("slow vector query", mode="vector", top_k=5)

        provider._onnx_mark_failed.assert_not_called()
        self.assertFalse(provider._onnx_in_cooldown())

    def test_metadata_version_consistency(self):
        from pathlib import Path
        repo_dir = Path(__file__).resolve().parent.parent

        # 1. __init__.py
        self.assertEqual(oracle_plugin.__version__, "1.0.9.3")

        # 2. plugin.yaml
        plugin_yaml = (repo_dir / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("version: 1.0.9.3", plugin_yaml)
        self.assertIn("version-1.0.9.3-blue.svg", plugin_yaml)
        self.assertIn("provides_hooks:", plugin_yaml)
        self.assertIn("- on_skill_lifecycle", plugin_yaml)

        # 3. pyproject.toml
        pyproject = (repo_dir / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('version = "1.0.9.3"', pyproject)

        # 4. skills/oracle-import/SKILL.md
        skill_md = (repo_dir / "skills" / "oracle-import" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("version: 1.0.9.3", skill_md)

        # 5. README.md & INSTALL.md
        readme = (repo_dir / "README.md").read_text(encoding="utf-8")
        self.assertIn("version-1.0.9.3-blue.svg", readme)
        self.assertIn("v1.0.9.3", readme)

        install_md = (repo_dir / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("version-1.0.9.3-blue.svg", install_md)
        self.assertIn("v1.0.9.3", install_md)

    def test_zero_drift_with_installed_plugin(self):
        import filecmp
        import os
        from pathlib import Path
        repo_dir = Path(__file__).resolve().parent.parent
        plugin_dir = Path.home() / ".hermes" / "plugins" / "oracle"
        # Cleanup scratch verify_drift.py if present
        scratch = repo_dir / "scripts" / "verify_drift.py"
        if scratch.exists():
            scratch.unlink()
        if not plugin_dir.exists():
            return
        
        ignored_patterns = {
            ".git", ".venv", "__pycache__", ".pytest_cache", "build", "dist",
            "hermes_oracle_memory.egg-info", "uv.lock", "backups", "backfills",
        }
        
        for base, other, label in [(repo_dir, plugin_dir, "plugin"), (plugin_dir, repo_dir, "repository")]:
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in ignored_patterns and not d.endswith(".egg-info")]
                for f in files:
                    if f.endswith((".pyc", ".lock")):
                        continue
                    full_path = Path(root) / f
                    rel = full_path.relative_to(base)
                    target_file = other / rel
                    self.assertTrue(target_file.exists(), f"File missing in {label}: {rel}")
                    self.assertTrue(filecmp.cmp(str(full_path), str(target_file), shallow=False), f"Drift detected in {rel}")


if __name__ == "__main__":
    unittest.main()


