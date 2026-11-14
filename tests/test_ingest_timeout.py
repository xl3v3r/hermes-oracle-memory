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

            # Must be >= search timeout (30000 ms default) and >= ingest timeout (600,000 ms default)
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

    # --- 1.1.1.3 timeout consolidation ---------------------------------------
    SECRETS = {"OCI_DB_USER": "ADMIN", "OCI_DB_PASSWORD": "***", "OCI_DB_DSN": "adb_dsn_high"}
    TIMEOUT_VARS = (
        "HERMES_ORACLE_SEARCH_TIMEOUT_MS", "HERMES_ORACLE_SEARCH_TIMEOUT", "HERMES_ORACLE_INGEST_TIMEOUT",
        "HERMES_ORACLE_POOL_WAIT_TIMEOUT_MS", "HERMES_ORACLE_POOL_IDLE_TIMEOUT", "HERMES_ORACLE_PREFETCH_WAIT",
    )

    def _init_with_env(self, env):
        clean = {k: v for k, v in os.environ.items() if k not in self.TIMEOUT_VARS}
        clean.update(env)
        saved = (oracle_plugin._SEARCH_TIMEOUT_MS, oracle_plugin._SEARCH_TIMEOUT_SECS,
                 oracle_plugin._INGEST_TIMEOUT_MS, oracle_plugin._INGEST_TIMEOUT_SECS)
        try:
            with patch.dict(os.environ, clean, clear=True), \
                 patch.object(oracle_plugin, "get_secret", side_effect=lambda k, *a: self.SECRETS.get(k)), \
                 patch("oracledb.SessionPool") as pool, \
                 patch.object(oracle_plugin.OracleMemoryProvider, "_ensure_schema"):
                provider = oracle_plugin.OracleMemoryProvider()
                provider.initialize("timeouts")
                return pool.call_args[1], oracle_plugin._SEARCH_TIMEOUT_MS, provider._pool_fingerprint
        finally:
            (oracle_plugin._SEARCH_TIMEOUT_MS, oracle_plugin._SEARCH_TIMEOUT_SECS,
             oracle_plugin._INGEST_TIMEOUT_MS, oracle_plugin._INGEST_TIMEOUT_SECS) = saved

    def test_defaults_pool_never_blocks_forever(self):
        import oracledb
        kw, search_ms, fp = self._init_with_env({})
        self.assertEqual(kw["getmode"], oracledb.POOL_GETMODE_TIMEDWAIT)
        self.assertEqual(kw["wait_timeout"], 20000)   # milliseconds
        self.assertEqual(kw["timeout"], 300)          # idle seconds
        self.assertEqual(kw["call_timeout"], 600000)
        self.assertEqual(search_ms, 30000)
        self.assertIn(":wt=20000:it=300", fp)

    def test_documented_ms_variable_drives_query_clamp(self):
        _, search_ms, _ = self._init_with_env({"HERMES_ORACLE_SEARCH_TIMEOUT_MS": "45000"})
        self.assertEqual(search_ms, 45000)

    def test_legacy_seconds_variable_still_honored(self):
        _, search_ms, _ = self._init_with_env({"HERMES_ORACLE_SEARCH_TIMEOUT": "20"})
        self.assertEqual(search_ms, 20000)
        _, search_ms, _ = self._init_with_env(
            {"HERMES_ORACLE_SEARCH_TIMEOUT": "20", "HERMES_ORACLE_SEARCH_TIMEOUT_MS": "25000"})
        self.assertEqual(search_ms, 25000)  # _MS wins

    def test_bad_values_fall_back_and_floats_parse(self):
        kw, search_ms, _ = self._init_with_env({
            "HERMES_ORACLE_SEARCH_TIMEOUT_MS": "soon",
            "HERMES_ORACLE_INGEST_TIMEOUT": "600.0",      # used to crash int()
            "HERMES_ORACLE_POOL_WAIT_TIMEOUT_MS": "-5",
            "HERMES_ORACLE_POOL_IDLE_TIMEOUT": "5",       # clamped up to 30
        })
        self.assertEqual(search_ms, 30000)
        self.assertEqual(kw["call_timeout"], 600000)
        self.assertEqual(kw["wait_timeout"], 20000)
        self.assertEqual(kw["timeout"], 30)

    def test_prefetch_wait_within_host_budget(self):
        self.assertLessEqual(oracle_plugin._PREFETCH_WAIT_SECS, 7.0)
        self.assertGreaterEqual(oracle_plugin._PREFETCH_WAIT_SECS, 1.0)

    def test_pool_exhaustion_is_not_an_onnx_failure(self):
        err = Exception("DPY-4005: timed out waiting for the connection pool to return a connection")
        self.assertTrue(oracle_plugin._is_session_error(err))
        self.assertFalse(oracle_plugin._is_onnx_model_failure(err))


if __name__ == "__main__":
    unittest.main()
