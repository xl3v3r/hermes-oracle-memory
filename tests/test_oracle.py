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
        self.assertEqual(provider._contains_query(None), "memory")

        # Test empty string
        self.assertEqual(provider._contains_query(""), "memory")

        # Test whitespace only
        self.assertEqual(provider._contains_query("   \n\t  "), "memory")

        # Test only punctuation
        self.assertEqual(provider._contains_query("?!.,;:()"), "memory")

        # Test only short words
        self.assertEqual(provider._contains_query("a to is"), "memory")

        # Test valid query
        self.assertEqual(provider._contains_query("test query"), "test ACCUM query")

        # Test hyphen
        self.assertEqual(provider._contains_query("top-secret info"), "top-secret ACCUM info")

        # Test punctuation
        self.assertEqual(provider._contains_query("hello, world! how's it going?"), "hello ACCUM world ACCUM how ACCUM going")

        # Test max tokens
        long_query = "one two three four five six seven eight nine ten"
        expected = "one ACCUM two ACCUM three ACCUM four ACCUM five ACCUM six ACCUM seven ACCUM eight"
        self.assertEqual(provider._contains_query(long_query), expected)

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

    def test_embed_openrouter_model_and_dimensions(self):
        self.assertEqual(oracle_plugin._EMBED_MODEL, "qwen/qwen3-embedding-4b")
        self.assertEqual(oracle_plugin._EMBED_DIM, 768)

        provider = oracle_plugin.OracleMemoryProvider()
        captured_req = {}

        class DummyResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                import json
                return json.dumps({
                    "data": [{"index": 0, "embedding": [0.1] * 768}]
                }).encode("utf-8")

        def fake_urlopen(req, timeout=20):
            captured_req["url"] = req.full_url
            captured_req["data"] = json.loads(req.data.decode("utf-8"))
            captured_req["headers"] = dict(req.headers)
            return DummyResponse()

        with patch.object(oracle_plugin, "get_secret", return_value="fake-openrouter-key"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = provider._embed_openrouter(["sample text to embed"])
            self.assertEqual(len(res), 1)
            self.assertIsNotNone(res[0])
            self.assertEqual(len(res[0]), 768)
            self.assertEqual(captured_req["data"]["model"], "qwen/qwen3-embedding-4b")
            self.assertEqual(captured_req["data"]["dimensions"], 768)
            self.assertEqual(captured_req["data"]["input"], ["sample text to embed"])

    def test_embed_openrouter_empty_none_and_whitespace(self):
        provider = oracle_plugin.OracleMemoryProvider()
        captured_req = {}

        class DummyResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                import json
                return json.dumps({
                    "data": [
                        {"index": 0, "embedding": [0.1] * 768},
                        {"index": 1, "embedding": [0.2] * 768},
                    ]
                }).encode("utf-8")

        def fake_urlopen(req, timeout=20):
            captured_req["data"] = json.loads(req.data.decode("utf-8"))
            return DummyResponse()

        with patch.object(oracle_plugin, "get_secret", return_value="fake-openrouter-key"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            inputs = ["valid one", "", "   ", None, "valid two"]
            res = provider._embed_openrouter(inputs)
            self.assertEqual(len(res), 5)
            self.assertIsNotNone(res[0])
            self.assertEqual(len(res[0]), 768)
            self.assertIsNone(res[1])
            self.assertIsNone(res[2])
            self.assertIsNone(res[3])
            self.assertIsNotNone(res[4])
            self.assertEqual(len(res[4]), 768)
            # Only valid, non-empty items sent to OpenRouter
            self.assertEqual(captured_req["data"]["input"], ["valid one", "valid two"])

    def test_embed_openrouter_all_empty(self):
        provider = oracle_plugin.OracleMemoryProvider()
        with patch.object(oracle_plugin, "get_secret", return_value="fake-openrouter-key"), \
             patch("urllib.request.urlopen") as mock_urlopen:
            res = provider._embed_openrouter(["", "   ", None])
            self.assertEqual(res, [None, None, None])
            mock_urlopen.assert_not_called()

    def test_embed_openrouter_dimension_mismatch(self):
        provider = oracle_plugin.OracleMemoryProvider()

        class DummyResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                import json
                return json.dumps({
                    "data": [
                        {"index": 0, "embedding": [0.1] * 512},  # wrong dim
                        {"index": 1, "embedding": [0.2] * 768}, # correct dim
                    ]
                }).encode("utf-8")

        with patch.object(oracle_plugin, "get_secret", return_value="fake-openrouter-key"), \
             patch("urllib.request.urlopen", return_value=DummyResponse()):
            res = provider._embed_openrouter(["doc1", "doc2"])
            self.assertEqual(len(res), 2)
            self.assertIsNone(res[0])  # Discarded due to 512 != 768
            self.assertIsNotNone(res[1])
            self.assertEqual(len(res[1]), 768)

    def test_embed_many_deduplicates_embedding_misses(self):
        import array

        provider = oracle_plugin.OracleMemoryProvider()
        provider._onnx_model = ""
        first = array.array("f", [0.1] * 768)
        second = array.array("f", [0.2] * 768)
        provider._embed_oci = MagicMock(return_value=None)
        provider._embed_openrouter = MagicMock(return_value=[first, second])

        result = provider._embed_many(["duplicate", "duplicate", "unique"])

        self.assertEqual(result, [first, first, second])
        provider._embed_openrouter.assert_called_once_with(["duplicate", "unique"])

    def test_embed_many_cools_down_after_onnx_failure(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._onnx_model = "BGE_BASE_EN_V1_5"
        provider._pool = MagicMock()
        provider._pool.acquire.side_effect = Exception("model unavailable")
        provider._embed_oci = MagicMock(return_value=None)
        provider._embed_openrouter = MagicMock(return_value=[None])

        provider._embed_many(["first"])
        provider._embed_many(["second"])

        self.assertEqual(provider._pool.acquire.call_count, 1)
        self.assertEqual(provider._embed_openrouter.call_count, 2)

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

    def test_embed_openrouter_retry_on_transient_error(self):
        provider = oracle_plugin.OracleMemoryProvider()
        import urllib.error

        class DummyResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                import json
                return json.dumps({
                    "data": [{"index": 0, "embedding": [0.3] * 768}]
                }).encode("utf-8")

        call_count = [0]
        def fake_urlopen_with_retry(req, timeout=20):
            call_count[0] += 1
            if call_count[0] == 1:
                fp = MagicMock()
                fp.read.return_value = b'{"error": "rate limit"}'
                raise urllib.error.HTTPError("https://openrouter.ai/api/v1/embeddings", 429, "Too Many Requests", {}, fp)
            return DummyResponse()

        with patch.object(oracle_plugin, "get_secret", return_value="fake-openrouter-key"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen_with_retry), \
             patch("time.sleep"):
            res = provider._embed_openrouter(["retry text"])
            self.assertEqual(call_count[0], 2)
            self.assertIsNotNone(res[0])
            self.assertEqual(len(res[0]), 768)

    def test_onnx_search_fallback_to_openrouter(self):
        import array
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        # Mock query failure on in-database ONNX call
        fake_fallback_vec = array.array("d", [0.1] * 768)
        provider._embed_external = MagicMock(return_value=[fake_fallback_vec])

        # Cur.fetchall raises error on ONNX query, then succeeds on fallback
        call_count = [0]
        def side_effect_fetch(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("ORA-29532: Java or ONNX call failed")
            return [("m1", "s1", "hermes", "user", "Memory text", "memory", 0.0, 0.2)]

        mock_cur.fetchall.side_effect = side_effect_fetch

        results = provider._search("database query", mode="vector")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["memory_id"], "m1")
        provider._embed_external.assert_called_with(["database query"])

    def test_onnx_insert_fallback_to_openrouter(self):
        import array
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        fake_fallback_vec = array.array("d", [0.2] * 768)
        provider._embed_external_tagged = MagicMock(
            return_value=([fake_fallback_vec], "openrouter:test-model")
        )

        # First cur.execute fails on ONNX INSERT, second succeeds
        call_count = [0]
        def side_effect_execute(sql, binds):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("ORA-40284: model does not exist")
            return None

        mock_cur.execute.side_effect = side_effect_execute

        provider._insert(
            memory_id="test_id",
            session_id="s1",
            role="user",
            content="test content",
            target="memory",
        )
        self.assertEqual(call_count[0], 2)
        provider._embed_external_tagged.assert_called_with(["test content"])
        # the fallback INSERT must stamp the external model, not leave it NULL
        fallback_sql, fallback_binds = mock_cur.execute.call_args[0]
        self.assertIn("embedding_model", fallback_sql)
        self.assertIn("openrouter:test-model", fallback_binds)

    def test_onnx_update_fallback_to_openrouter(self):
        import array
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = "BGE_BASE_EN_V1_5"

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.rowcount = 1
        provider._pool = MagicMock()
        provider._pool.acquire.return_value = mock_conn

        fake_fallback_vec = array.array("d", [0.3] * 768)
        provider._embed_external_tagged = MagicMock(
            return_value=([fake_fallback_vec], "openrouter:test-model")
        )

        call_count = [0]
        def side_effect_execute(sql, binds):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("ORA-40284: model does not exist")
            return None

        mock_cur.execute.side_effect = side_effect_execute

        res = provider.handle_tool_call("oracle_update", {"memory_id": "m1", "content": "updated content"})
        self.assertIn('"result": "updated"', res)
        self.assertEqual(call_count[0], 2)
        # the fallback UPDATE stamps the external model it actually used
        fallback_sql, fallback_binds = mock_cur.execute.call_args[0]
        self.assertIn("embedding_model", fallback_sql)
        self.assertIn("openrouter:test-model", fallback_binds)

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
        provider._onnx_model = ""
        provider._pool = MagicMock()
        provider._embed_external_tagged = MagicMock(
            return_value=([array.array("d", [0.1] * 768)], "openrouter:test-model")
        )
        mock_conn, mock_cur = self._mocked_conn()
        provider._pool.acquire.return_value = mock_conn
        provider._embed_pending.append(("m1", "queued content"))

        provider._embed_worker_loop()

        sql, binds_list = mock_cur.executemany.call_args[0]
        self.assertIn("embedding_model", sql)
        self.assertEqual(binds_list[0][1], "openrouter:test-model")

    def test_embed_many_tagged_reports_backend(self):
        import array
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = ""  # force the client-side external path
        provider._pool = MagicMock()
        fake_vec = array.array("d", [0.1] * 768)
        provider._embed_external_tagged = MagicMock(
            return_value=([fake_vec], "openrouter:test-model")
        )

        result = provider._embed_many_tagged(["hello", "hello"])
        self.assertEqual([v for v, _m in result], [fake_vec, fake_vec])
        self.assertEqual([m for _v, m in result], ["openrouter:test-model"] * 2)
        # the untagged view stays compatible with search-only callers
        self.assertEqual(provider._embed_many(["hello"]), [fake_vec])

    def test_embed_cache_preserves_provenance(self):
        import array
        provider = oracle_plugin.OracleMemoryProvider()
        provider._table = "HERMES_AGENT_MEMORY"
        provider._onnx_model = ""
        provider._pool = MagicMock()
        fake_vec = array.array("d", [0.1] * 768)
        tagged_mock = MagicMock(return_value=([fake_vec], "openrouter:test-model"))
        provider._embed_external_tagged = tagged_mock

        provider._embed_many_tagged(["same text"])
        provider._embed_many_tagged(["same text"])
        self.assertEqual(tagged_mock.call_count, 1)  # second served from cache
        result = provider._embed_many_tagged(["same text"])
        self.assertEqual(result[0][1], "openrouter:test-model")

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


if __name__ == "__main__":
    unittest.main()
