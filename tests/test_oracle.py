import sys
import importlib.util
import os
import unittest
from unittest.mock import MagicMock, patch

# Get path to root __init__.py
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
init_path = os.path.join(root_dir, "__init__.py")

spec = importlib.util.spec_from_file_location("oracle_plugin", init_path)
oracle_plugin = importlib.util.module_from_spec(spec)
sys.modules["oracle_plugin"] = oracle_plugin
spec.loader.exec_module(oracle_plugin)


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
        with patch.object(oracle_plugin, "get_secret", return_value=MagicMock()):
            reason = provider.unavailable_reason()
            self.assertIn("missing env", reason)

    def test_is_internal_gateway_turn_handles_magicmock(self):
        self.assertFalse(oracle_plugin._is_internal_gateway_turn(MagicMock()))


if __name__ == "__main__":
    unittest.main()
