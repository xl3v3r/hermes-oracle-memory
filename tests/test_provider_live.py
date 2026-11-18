"""Live database checks, opt-in.

They run against the database named by OCI_DB_USER, OCI_DB_PASSWORD and
OCI_DB_DSN, and only when HERMES_ORACLE_LIVE_TESTS=1 is set. Credentials in the
environment, or an env file on disk, are not enough: a plain `pytest` run never
connects to a database. Every test deletes the rows it writes.

    HERMES_ORACLE_LIVE_TESTS=1 python -m pytest -q tests/test_provider_live.py
"""

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401  (stubs agent.* / tools.*)
import tests.verify_provider as verify_provider  # noqa: E402

_SKIP = (
    "set HERMES_ORACLE_LIVE_TESTS=1 (with OCI_DB_USER, OCI_DB_PASSWORD and OCI_DB_DSN) "
    "to run against a database"
)


def _env_secret(key, default=None):
    return os.environ.get(key, default)


class TestProviderLive(unittest.TestCase):
    def setUp(self):
        if os.environ.get("HERMES_ORACLE_LIVE_TESTS") != "1":
            self.skipTest(_SKIP)

    def test_verify_provider_live(self):
        self.assertEqual(verify_provider.main([]), 0, "tests/verify_provider.py reported a failure")

    def test_imported_assistant_message_is_found_by_search(self):
        """#20: an imported archive is stored under a role that search returns."""
        from scripts import import_conversations_ordered as importer

        marker = uuid.uuid4().hex[:12]
        session_id = f"verify-import-{marker}"
        answer = f"The harbor relay certificate {marker} expired on the first of March."
        archive = [{
            "name": f"Relay outage {marker}",
            "summary": "Why the relay dropped connections",
            "chat_messages": [
                {"sender": "human", "text": "Why did the relay drop?", "created_at": "2026-01-02T10:00:00Z"},
                {"sender": "assistant", "text": answer, "created_at": "2026-01-02T10:00:05Z"},
            ],
        }]
        plugin = importer.common.load_plugin()
        with patch.object(plugin, "get_secret", _env_secret), tempfile.TemporaryDirectory() as td:
            path = Path(td) / "conversations.json"
            path.write_text(json.dumps(archive), encoding="utf-8")
            provider = plugin.OracleMemoryProvider()
            try:
                self.assertEqual(importer.main([str(path), "--session-id", session_id]), 0)
                provider.initialize(session_id)
                self.assertIsNotNone(provider._pool, provider._init_error)
                found = json.loads(provider.handle_tool_call(
                    "oracle_search", {"query": answer, "mode": "vector", "top_k": 8, "session_id": session_id}))
                rows = found.get("results") or []
                hit = [r for r in rows if marker in (r.get("content") or "") and "expired" in (r.get("content") or "")]
                self.assertTrue(hit, found)
                self.assertEqual(hit[0]["role"], "chunk")
                self.assertEqual(hit[0]["target"], "memory")
                self.assertEqual(len(rows), 3, found)  # summary, question, answer
            finally:
                if provider._pool is None:
                    provider.initialize(session_id)
                if provider._pool is not None:
                    with provider._pool.acquire() as conn, conn.cursor() as cur:
                        cur.execute(f"DELETE FROM {provider._table} WHERE session_id = :sid", {"sid": session_id})  # nosec B608
                        conn.commit()
                        cur.execute(f"SELECT COUNT(*) FROM {provider._table} WHERE session_id = :sid", {"sid": session_id})  # nosec B608
                        left = cur.fetchone()[0]
                    provider.shutdown()
                    self.assertEqual(left, 0)


if __name__ == "__main__":
    unittest.main()
