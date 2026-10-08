import sys
import unittest
from unittest.mock import MagicMock, patch

# Move module mocks to module level so pytest imports them before parsing the test cases
sys.modules["agent"] = MagicMock()
agent_mp = MagicMock()
agent_mp.MemoryProvider = object
agent_mp.RecallStatus = MagicMock()
agent_mp.is_trivial_prompt = MagicMock()
sys.modules["agent.memory_provider"] = agent_mp
sys.modules["agent.secret_scope"] = MagicMock()
sys.modules["tools"] = MagicMock()
sys.modules["tools.registry"] = MagicMock()

import __init__

class TestOracleMemoryProvider(unittest.TestCase):
    def test_unavailable_reason_no_oracledb(self):
        provider = __init__.OracleMemoryProvider()
        # Mock 'import oracledb' raising an ImportError
        with patch.dict('sys.modules', {'oracledb': None}):
            with patch('builtins.__import__') as mock_import:
                def mock_import_func(name, *args, **kwargs):
                    if name == 'oracledb':
                        raise ImportError("No module named 'oracledb'")
                    return __import__(name, *args, **kwargs)
                mock_import.side_effect = mock_import_func

                self.assertEqual(provider.unavailable_reason(), "oracledb is not installed in the Hermes venv")

    def test_unavailable_reason_missing_one_secret(self):
        provider = __init__.OracleMemoryProvider()
        # Provide a mock oracledb
        mock_oracledb = MagicMock()
        with patch.dict('sys.modules', {'oracledb': mock_oracledb}):
            def mock_get_secret(key, default=None):
                secrets = {
                    "OCI_DB_USER": "user",
                    "OCI_DB_DSN": "dsn",
                }
                return secrets.get(key)

            with patch('__init__.get_secret', side_effect=mock_get_secret):
                self.assertEqual(provider.unavailable_reason(), "missing env: OCI_DB_PASSWORD")

    def test_unavailable_reason_missing_multiple_secrets(self):
        provider = __init__.OracleMemoryProvider()
        mock_oracledb = MagicMock()
        with patch.dict('sys.modules', {'oracledb': mock_oracledb}):
            def mock_get_secret(key, default=None):
                secrets = {
                    "OCI_DB_USER": "user",
                }
                return secrets.get(key)

            with patch('__init__.get_secret', side_effect=mock_get_secret):
                self.assertEqual(provider.unavailable_reason(), "missing env: OCI_DB_PASSWORD, OCI_DB_DSN")

    def test_unavailable_reason_missing_all_secrets(self):
        provider = __init__.OracleMemoryProvider()
        mock_oracledb = MagicMock()
        with patch.dict('sys.modules', {'oracledb': mock_oracledb}):
            with patch('__init__.get_secret', return_value=None):
                self.assertEqual(provider.unavailable_reason(), "missing env: OCI_DB_USER, OCI_DB_PASSWORD, OCI_DB_DSN")

    def test_unavailable_reason_all_present(self):
        provider = __init__.OracleMemoryProvider()
        mock_oracledb = MagicMock()
        with patch.dict('sys.modules', {'oracledb': mock_oracledb}):
            with patch('__init__.get_secret', return_value="secret_val"):
                self.assertEqual(provider.unavailable_reason(), "")

if __name__ == '__main__':
    unittest.main()
