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


if __name__ == "__main__":
    unittest.main()

