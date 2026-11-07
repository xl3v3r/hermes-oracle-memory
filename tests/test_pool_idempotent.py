import os
import sys
import unittest
from unittest.mock import MagicMock, patch

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import tests.conftest
import __init__ as oracle_plugin


class TestPoolIdempotent(unittest.TestCase):
    def test_double_initialize_creates_only_one_pool(self):
        """Verify calling initialize() multiple times with identical config does not recreate the pool."""
        provider = oracle_plugin.OracleMemoryProvider()

        secrets = {
            "OCI_DB_USER": "ADMIN",
            "OCI_DB_PASSWORD": "ValidPassword123",
            "OCI_DB_DSN": "adb_dsn_high",
            "HERMES_ORACLE_TABLE": "hermes_agent_memory",
        }

        mock_pool = MagicMock()
        with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, *args: secrets.get(k)), \
             patch("oracledb.SessionPool", return_value=mock_pool) as mock_session_pool, \
             patch.object(provider, "_ensure_schema"):

            # First initialize
            provider.initialize("session-1")
            self.assertTrue(provider._initialized)
            self.assertIsNotNone(provider._pool)
            mock_session_pool.assert_called_once()

            # Second initialize with same config
            provider.initialize("session-2")
            # SessionPool constructor should NOT have been called a second time
            mock_session_pool.assert_called_once()
            self.assertEqual(provider._session_id, "session-2")

    def test_queue_background_insert_before_initialize_drops_safely(self):
        """Verify _queue_background_insert before init drops silently without raising or leaking memory."""
        provider = oracle_plugin.OracleMemoryProvider()
        self.assertIsNone(provider._pool)
        self.assertFalse(provider._initialized)

        # Should not raise an exception
        try:
            for i in range(10):
                provider._queue_background_insert(
                    memory_id=f"mem-{i}",
                    session_id="test-session",
                    role="turn",
                    content=f"Turn content {i}",
                    target="memory",
                    metadata={"kind": "turn"},
                )
        except Exception as e:
            self.fail(f"_queue_background_insert raised an exception when uninitialized: {e}")

        # Provider background threads or pending queues must remain clean
        self.assertEqual(len(provider._sync_threads), 0)
        self.assertTrue(provider._bg_drop_warned)

    def test_health_shape_and_values(self):
        """Verify health() returns the required diagnostic keys: pool, rows, last_write, init_error."""
        provider = oracle_plugin.OracleMemoryProvider()

        # When uninitialized
        h_uninit = provider.health()
        self.assertIsInstance(h_uninit, dict)
        self.assertIn("pool", h_uninit)
        self.assertIn("rows", h_uninit)
        self.assertIn("last_write", h_uninit)
        self.assertIn("init_error", h_uninit)
        self.assertFalse(h_uninit["pool"])
        self.assertEqual(h_uninit["rows"], 0)
        self.assertEqual(h_uninit["last_write"], "none")

        # When initialized and active
        provider._pool = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        provider._pool.acquire.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = (42,)
        provider._last_write_at = "2026-11-06T12:30:00Z"

        h_active = provider.health()
        self.assertTrue(h_active["pool"])
        self.assertEqual(h_active["rows"], 42)
        self.assertEqual(h_active["last_write"], "2026-11-06T12:30:00Z")
        self.assertIsNone(h_active["init_error"])


if __name__ == "__main__":
    unittest.main()
