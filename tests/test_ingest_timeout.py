import os
import sys
import unittest
from unittest.mock import MagicMock, patch

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import tests.conftest
import __init__ as oracle_plugin


class TestIngestTimeout(unittest.TestCase):
    def test_pool_created_with_call_timeout_at_least_search_timeout(self):
        """Verify SessionPool is created with call_timeout >= search timeout."""
        provider = oracle_plugin.OracleMemoryProvider()
        secrets = {
            "OCI_DB_USER": "ADMIN",
            "OCI_DB_PASSWORD": "ValidPassword123",
            "OCI_DB_DSN": "adb_dsn_high",
        }

        with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, *args: secrets.get(k)), \
             patch("oracledb.SessionPool") as mock_session_pool, \
             patch.object(provider, "_ensure_schema"):

            provider.initialize("session-timeout-check")
            self.assertTrue(mock_session_pool.called)
            kwargs = mock_session_pool.call_args[1]

            self.assertIn("call_timeout", kwargs)
            call_timeout = kwargs["call_timeout"]

            # Must be >= search timeout (1300 ms) and equal to ingest timeout (600,000 ms by default)
            self.assertGreaterEqual(call_timeout, oracle_plugin._SEARCH_TIMEOUT_MS)
            self.assertGreaterEqual(call_timeout, 600000)

    def test_env_override_ingest_timeout_honored(self):
        """Verify HERMES_ORACLE_INGEST_TIMEOUT environment variable override is honored."""
        custom_timeout_seconds = "900"
        with patch.dict(os.environ, {"HERMES_ORACLE_INGEST_TIMEOUT": custom_timeout_seconds}):
            # Calculate expected timeout in milliseconds
            expected_ms = int(custom_timeout_seconds) * 1000

            provider = oracle_plugin.OracleMemoryProvider()
            secrets = {
                "OCI_DB_USER": "ADMIN",
                "OCI_DB_PASSWORD": "ValidPassword123",
                "OCI_DB_DSN": "adb_dsn_high",
            }

            with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, *args: secrets.get(k)), \
                 patch("oracledb.SessionPool") as mock_session_pool, \
                 patch.object(provider, "_ensure_schema"):

                provider.initialize("session-env-override")
                kwargs = mock_session_pool.call_args[1]
                self.assertEqual(kwargs["call_timeout"], expected_ms)


if __name__ == "__main__":
    unittest.main()
