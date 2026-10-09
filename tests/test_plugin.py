import os
import sys
import unittest
from unittest.mock import MagicMock, patch

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import tests.conftest
import __init__ as oracle_plugin


class TestOracleMemoryProvider(unittest.TestCase):
    def test_get_tool_schemas(self):
        provider = oracle_plugin.OracleMemoryProvider()
        schemas = provider.get_tool_schemas()

        self.assertIsInstance(schemas, list)
        self.assertEqual(len(schemas), len(oracle_plugin.TOOL_SCHEMAS))
        self.assertEqual(schemas, oracle_plugin.TOOL_SCHEMAS)
        # Verify we get a new list (to prevent accidental mutation of the original)
        self.assertIsNot(schemas, oracle_plugin.TOOL_SCHEMAS)

    def test_get_tool_schemas_content(self):
        provider = oracle_plugin.OracleMemoryProvider()
        schemas = provider.get_tool_schemas()

        expected_names = {"oracle_search", "oracle_add", "oracle_update", "oracle_delete", "oracle_chunk"}
        actual_names = {schema["name"] for schema in schemas}
        self.assertEqual(actual_names, expected_names)

    def test_unavailable_reason_no_oracledb(self):
        provider = oracle_plugin.OracleMemoryProvider()
        with patch.dict('sys.modules', {'oracledb': None}):
            with patch('builtins.__import__') as mock_import:
                def mock_import_func(name, *args, **kwargs):
                    if name == 'oracledb':
                        raise ImportError("No module named 'oracledb'")
                    return __import__(name, *args, **kwargs)
                mock_import.side_effect = mock_import_func

                self.assertEqual(provider.unavailable_reason(), "oracledb is not installed in the Hermes venv")

    def test_unavailable_reason_missing_one_secret(self):
        provider = oracle_plugin.OracleMemoryProvider()
        mock_oracledb = MagicMock()
        with patch.dict('sys.modules', {'oracledb': mock_oracledb}):
            def mock_get_secret(key, default=None):
                secrets = {
                    "OCI_DB_USER": "user",
                    "OCI_DB_DSN": "dsn",
                }
                return secrets.get(key)

            with patch.object(oracle_plugin, 'get_secret', side_effect=mock_get_secret):
                self.assertEqual(provider.unavailable_reason(), "missing env: OCI_DB_PASSWORD")

    def test_unavailable_reason_missing_multiple_secrets(self):
        provider = oracle_plugin.OracleMemoryProvider()
        mock_oracledb = MagicMock()
        with patch.dict('sys.modules', {'oracledb': mock_oracledb}):
            def mock_get_secret(key, default=None):
                secrets = {
                    "OCI_DB_USER": "user",
                }
                return secrets.get(key)

            with patch.object(oracle_plugin, 'get_secret', side_effect=mock_get_secret):
                self.assertEqual(provider.unavailable_reason(), "missing env: OCI_DB_PASSWORD, OCI_DB_DSN")

    def test_unavailable_reason_missing_all_secrets(self):
        provider = oracle_plugin.OracleMemoryProvider()
        mock_oracledb = MagicMock()
        with patch.dict('sys.modules', {'oracledb': mock_oracledb}):
            with patch.object(oracle_plugin, 'get_secret', return_value=None):
                self.assertEqual(provider.unavailable_reason(), "missing env: OCI_DB_USER, OCI_DB_PASSWORD, OCI_DB_DSN")

    def test_unavailable_reason_all_present(self):
        provider = oracle_plugin.OracleMemoryProvider()
        mock_oracledb = MagicMock()
        with patch.dict('sys.modules', {'oracledb': mock_oracledb}):
            with patch.object(oracle_plugin, 'get_secret', return_value="secret_val"):
                self.assertEqual(provider.unavailable_reason(), "")


if __name__ == '__main__':
    unittest.main()
