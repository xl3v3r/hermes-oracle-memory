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

    # -- Oracle-first behavior and _low pool tests --------------------------------

    def test_version(self):
        self.assertEqual(oracle_plugin.__version__, "1.0.8.1")

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
                self.assertEqual(pool_kwargs["min"], 1)
                self.assertEqual(pool_kwargs["max"], 3)
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

    def test_metadata_version_consistency(self):
        from pathlib import Path
        repo_dir = Path(__file__).resolve().parent.parent

        # 1. __init__.py
        self.assertEqual(oracle_plugin.__version__, "1.0.8.1")

        # 2. plugin.yaml
        plugin_yaml = (repo_dir / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("version: 1.0.8.1", plugin_yaml)
        self.assertIn("version-1.0.8.1-blue.svg", plugin_yaml)

        # 3. pyproject.toml
        pyproject = (repo_dir / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('version = "1.0.8.1"', pyproject)

        # 4. skills/oracle-import/SKILL.md
        skill_md = (repo_dir / "skills" / "oracle-import" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("version: 1.0.8.1", skill_md)

        # 5. README.md & INSTALL.md
        readme = (repo_dir / "README.md").read_text(encoding="utf-8")
        self.assertIn("version-1.0.8.1-blue.svg", readme)
        self.assertIn("v1.0.8.1", readme)

        install_md = (repo_dir / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("version-1.0.8.1-blue.svg", install_md)
        self.assertIn("v1.0.8.1", install_md)

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


