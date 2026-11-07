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
        self.provider._pool.acquire.return_value.__enter__.return_value = self.mock_conn
        self.mock_conn.cursor.return_value.__enter__.return_value = self.mock_cur

    def test_create_and_patch_adds_rows_matching_format_skill_payload(self):
        """Verify on_skill_lifecycle('created'/'patched') resolves local file and inserts payload rows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
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
            skills_dir = Path(tmpdir) / "skills"
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
            skills_dir = Path(tmpdir) / "skills"
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
                self.assertEqual(binds["s"], "arch-skill")

    def test_foreign_host_rows_never_touched(self):
        """Verify delete query strictly scopes to host_id and local host root prefix."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            # No local file exists for foreign skill
            with patch("pathlib.Path.home", return_value=Path(tmpdir)), \
                 patch.dict(os.environ, {"HERMES_HOST_ID": "atlas_host"}):

                self.provider.on_skill_lifecycle("archived", "foreign-skill")
                time.sleep(0.1)

                delete_calls = [
                    c for c in self.mock_cur.execute.call_args_list
                    if "DELETE FROM" in str(c[0][0])
                ]
                self.assertEqual(len(delete_calls), 1)
                sql = delete_calls[0][0][0]
                binds = delete_calls[0][0][1]

                # SQL must gate deletion to current host
                self.assertIn("json_value(metadata, '$.host') = :h", sql)
                self.assertEqual(binds["h"], "atlas_host")


if __name__ == "__main__":
    unittest.main()
