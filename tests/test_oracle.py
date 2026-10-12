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
        self.assertEqual(oracle_plugin._EMBED_DIM, 1536)

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
                    "data": [{"index": 0, "embedding": [0.1] * 1536}]
                }).encode("utf-8")

        def fake_urlopen(req, timeout=20):
            import json
            captured_req["url"] = req.full_url
            captured_req["data"] = json.loads(req.data.decode("utf-8"))
            captured_req["headers"] = dict(req.headers)
            return DummyResponse()

        with patch.object(oracle_plugin, "get_secret", return_value="fake-openrouter-key"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = provider._embed_openrouter(["sample text to embed"])
            self.assertEqual(len(res), 1)
            self.assertIsNotNone(res[0])
            self.assertEqual(len(res[0]), 1536)
            self.assertEqual(captured_req["data"]["model"], "qwen/qwen3-embedding-4b")
            self.assertEqual(captured_req["data"]["dimensions"], 1536)
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
                        {"index": 0, "embedding": [0.1] * 1536},
                        {"index": 1, "embedding": [0.2] * 1536},
                    ]
                }).encode("utf-8")

        def fake_urlopen(req, timeout=20):
            import json
            captured_req["data"] = json.loads(req.data.decode("utf-8"))
            return DummyResponse()

        with patch.object(oracle_plugin, "get_secret", return_value="fake-openrouter-key"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            inputs = ["valid one", "", "   ", None, "valid two"]
            res = provider._embed_openrouter(inputs)
            self.assertEqual(len(res), 5)
            self.assertIsNotNone(res[0])
            self.assertEqual(len(res[0]), 1536)
            self.assertIsNone(res[1])
            self.assertIsNone(res[2])
            self.assertIsNone(res[3])
            self.assertIsNotNone(res[4])
            self.assertEqual(len(res[4]), 1536)
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
                        {"index": 1, "embedding": [0.2] * 1536}, # correct dim
                    ]
                }).encode("utf-8")

        with patch.object(oracle_plugin, "get_secret", return_value="fake-openrouter-key"), \
             patch("urllib.request.urlopen", return_value=DummyResponse()):
            res = provider._embed_openrouter(["doc1", "doc2"])
            self.assertEqual(len(res), 2)
            self.assertIsNone(res[0])  # Discarded due to 512 != 1536
            self.assertIsNotNone(res[1])
            self.assertEqual(len(res[1]), 1536)

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
                    "data": [{"index": 0, "embedding": [0.3] * 1536}]
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
            self.assertEqual(len(res[0]), 1536)


if __name__ == "__main__":
    unittest.main()


