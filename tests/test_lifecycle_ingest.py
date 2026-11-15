import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import tests.conftest
import __init__ as oracle_plugin
from scripts.sync_skills import format_skill_payload, parse_skill_file


class TestLifecycleIngest(unittest.TestCase):
    def setUp(self):
        self.provider = oracle_plugin.OracleMemoryProvider()
        self.provider._pool = MagicMock()
        self.mock_conn = MagicMock()
        self.mock_cur = MagicMock()
        # Writers use _acquire_checked() (pool.acquire() without "with"); other
        # paths use "with pool.acquire() as conn": both get mock_conn.
        self.provider._pool.acquire.return_value = self.mock_conn
        self.mock_conn.__enter__.return_value = self.mock_conn
        self.mock_conn.cursor.return_value.__enter__.return_value = self.mock_cur

    def test_create_and_patch_adds_rows_matching_format_skill_payload(self):
        """Verify on_skill_lifecycle('created'/'patched') resolves local file and inserts payload rows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / ".hermes" / "skills"
            skill_folder = skills_dir / "my-test-skill"
            skill_folder.mkdir(parents=True)
            skill_file = skill_folder / "SKILL.md"
            skill_content = (
                "---\n"
                "name: my-test-skill\n"
                "description: A dynamic test skill\n"
                "version: 1.0.0\n"
                "---\n\n"
                "# My Test Skill\n"
                "Body content for testing live lifecycle ingestion."
            )
            skill_file.write_text(skill_content, encoding="utf-8")

            # Mock discovery candidate paths to include temp directory
            with patch("pathlib.Path.home", return_value=Path(tmpdir)), \
                 patch.object(self.provider, "_insert_many") as mock_insert_many:

                # Existing hash does not match -> should ingest
                self.mock_cur.fetchone.return_value = ("different_hash_123",)

                self.provider.on_skill_lifecycle("created", "my-test-skill")

                # Allow background worker thread to run
                time.sleep(0.1)

                self.assertTrue(mock_insert_many.called)
                rows_passed = mock_insert_many.call_args[0][0]

                # Check rows match format_skill_payload
                skill_info = parse_skill_file(skill_file)
                expected_rows = format_skill_payload(skill_info, mode="full", host=oracle_plugin.get_host_id())
                self.assertEqual(len(rows_passed), len(expected_rows))
                self.assertEqual(rows_passed[0]["content"], expected_rows[0]["content"])

    def test_unchanged_hash_is_noop_zero_db_calls(self):
        """Verify when local file hash equals the database c0 hash, zero DB writes occur."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / ".hermes" / "skills"
            skill_folder = skills_dir / "noop-skill"
            skill_folder.mkdir(parents=True)
            skill_file = skill_folder / "SKILL.md"
            skill_file.write_text("# No-op Skill\nUnchanged text", encoding="utf-8")

            skill_info = parse_skill_file(skill_file)

            with patch("pathlib.Path.home", return_value=Path(tmpdir)), \
                 patch.object(self.provider, "_insert_many") as mock_insert_many:

                # Database hash matches local file hash exactly
                self.mock_cur.fetchone.return_value = (skill_info.hash,)

                self.provider.on_skill_lifecycle("edited", "noop-skill")
                time.sleep(0.1)

                # _insert_many should NOT be called
                mock_insert_many.assert_not_called()

    def test_unresolvable_name_never_writes(self):
        """Verify skills not found on local disk log debug and exit without DB calls or exceptions."""
        with patch.object(self.provider, "_insert_many") as mock_insert_many:
            self.provider.on_skill_lifecycle("created", "completely-unknown-skill")
            time.sleep(0.1)
            mock_insert_many.assert_not_called()

    def test_archive_deletes_only_when_local_file_is_gone(self):
        """Verify archive deletes from DB iff file is absent; skips delete if file is still present."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / ".hermes" / "skills"
            skill_folder = skills_dir / "arch-skill"
            skill_folder.mkdir(parents=True)
            skill_file = skill_folder / "SKILL.md"
            skill_file.write_text("# Archive Skill", encoding="utf-8")

            with patch("pathlib.Path.home", return_value=Path(tmpdir)):
                # Case A: File still exists -> should NOT delete
                self.mock_cur.reset_mock()
                self.provider.on_skill_lifecycle("archived", "arch-skill")
                time.sleep(0.1)

                delete_calls_a = [
                    c for c in self.mock_cur.execute.call_args_list
                    if "DELETE FROM" in str(c[0][0])
                ]
                self.assertEqual(len(delete_calls_a), 0)

                # Case B: File is removed (retired) -> SHOULD delete
                skill_file.unlink()
                self.mock_cur.reset_mock()
                self.provider.on_skill_lifecycle("archived", "arch-skill")
                time.sleep(0.1)

                delete_calls_b = [
                    c for c in self.mock_cur.execute.call_args_list
                    if "DELETE FROM" in str(c[0][0])
                ]
                self.assertEqual(len(delete_calls_b), 1)
                del_sql, binds = delete_calls_b[0][0][0], delete_calls_b[0][0][1]
                host = oracle_plugin.get_host_id()
                self.assertEqual(binds["pfx"], f"skill:{host}:arch-skill:c%".replace("_", "\\_"))

    def test_foreign_host_rows_never_touched(self):
        """The archive delete is scoped to this host's own id prefix: other
        hosts' copies and the host-less shared corpus are never matched."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("pathlib.Path.home", return_value=Path(tmpdir)), \
                 patch.dict(os.environ, {"HERMES_HOST_ID": "peer_host"}):

                self.provider.on_skill_lifecycle("archived", "foreign-skill")
                time.sleep(0.1)

                delete_calls = [
                    c for c in self.mock_cur.execute.call_args_list
                    if "DELETE FROM" in str(c[0][0])
                ]
                self.assertEqual(len(delete_calls), 1)
                sql = delete_calls[0][0][0]
                binds = delete_calls[0][0][1]

                self.assertIn("memory_id LIKE :pfx ESCAPE", sql)
                self.assertNotIn("conv_ref", sql)
                self.assertNotIn("$.path", sql)
                # '_' escaped: peer_host must not match e.g. peerXhost
                self.assertEqual(binds, {"pfx": "skill:peer\\_host:foreign-skill:c%"})


class TestLifecycleUpsertAtomic(unittest.TestCase):
    """DELETE + re-INSERT of a skill is one transaction, and no writer starts
    once shutdown() has begun (a skill must never vanish from the shared corpus)."""

    def setUp(self):
        self.provider = oracle_plugin.OracleMemoryProvider()
        self.provider._pool = MagicMock()
        self.conn = MagicMock()
        self.cur = MagicMock()
        self.provider._pool.acquire.return_value = self.conn  # _acquire_checked()
        self.conn.__enter__.return_value = self.conn          # plain `with pool.acquire()`
        self.conn.cursor.return_value.__enter__.return_value = self.cur
        self.cur.fetchone.return_value = ("different_hash",)
        self.events = []
        self.cur.execute.side_effect = lambda sql, *a, **k: self.events.append(
            "delete" if sql.lstrip().upper().startswith("DELETE") else "sql")
        self.conn.commit.side_effect = lambda: self.events.append("commit")
        self.conn.rollback.side_effect = lambda: self.events.append("rollback")
        # run the writer inline instead of on a thread
        self._sync = patch.object(self.provider, "_start_sync_thread",
                                  side_effect=lambda t: (t.run(), True)[1])
        self._sync.start()
        self.addCleanup(self._sync.stop)

    def _fire(self, insert_side_effect):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / ".hermes" / "skills" / "atomic-skill"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("---\nname: atomic-skill\ndescription: d\n---\n\n# Atomic\nBody.\n")
            with patch("pathlib.Path.home", return_value=Path(tmpdir)), \
                    patch.object(self.provider, "_insert_many", side_effect=insert_side_effect) as ins:
                self.provider.on_skill_lifecycle("edited", "atomic-skill")
                return ins

    def test_delete_and_insert_commit_together(self):
        ins = self._fire(lambda rows, tx_conn=None: (self.events.append("insert"), [])[1])
        self.assertEqual(ins.call_args.kwargs["tx_conn"], self.conn)
        self.assertEqual([e for e in self.events if e != "sql"], ["delete", "insert", "commit"])
        self.assertNotIn("atomic-skill", self.provider._in_flight_lifecycle_skills)

    def test_failed_insert_rolls_back_the_delete(self):
        def boom(rows, tx_conn=None):
            raise RuntimeError("In-database ONNX embedding model is unavailable")
        self._fire(boom)
        self.assertIn("rollback", self.events)
        self.assertNotIn("commit", self.events)
        self.provider._pool.drop.assert_called_once_with(self.conn)  # non-ORA error: session dropped
        self.assertNotIn("atomic-skill", self.provider._in_flight_lifecycle_skills)

    def test_pool_cleared_mid_upsert_still_commits_on_captured_connection(self):
        def clear_pool(rows, tx_conn=None):
            self.provider._pool = None  # shutdown() meanwhile
            return []
        self._fire(clear_pool)
        self.assertEqual(self.events[-1], "commit")

    def test_backfill_queued_only_after_commit(self):
        order = []
        self.conn.commit.side_effect = lambda: order.append("commit")
        with patch.object(self.provider, "_queue_embed_backfill",
                          side_effect=lambda m, c: order.append(("backfill", m))):
            self._fire(lambda rows, tx_conn=None: [("skill:x:c0", "body")])
        self.assertEqual(order, ["commit", ("backfill", "skill:x:c0")])

    def test_no_writer_after_shutdown_began(self):
        self._sync.stop()  # use the real _start_sync_thread
        self.provider._write_stop.set()
        with patch.object(oracle_plugin.threading.Thread, "start") as start:
            ins = self._fire(lambda rows, tx_conn=None: [])
        start.assert_not_called()
        ins.assert_not_called()
        self.assertNotIn("delete", self.events)
        self.assertNotIn("atomic-skill", self.provider._in_flight_lifecycle_skills)
        self._sync.start()


if __name__ == "__main__":
    unittest.main()
