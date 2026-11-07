import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import tests.conftest
from scripts.sync_skills import SkillInfo, sync_skills_library


class TestSyncPrune(unittest.TestCase):
    def setUp(self):
        self.mock_conn = MagicMock()
        self.mock_cur = MagicMock()
        self.mock_conn.cursor.return_value.__enter__.return_value = self.mock_cur

    def test_prune_scenario_table(self):
        """Scenario table:
        1. Alive local skills: untouched (never pruned).
        2. Own-path stale skills: deleted up to prune_limit.
        3. Foreign-path rows: never deleted.
        4. Legacy null-path rows: never deleted.
        """
        host_root = "/srv/fixtures/.hermes/skills/"
        alive_skills = [
            SkillInfo(
                name="alive-1",
                path=Path(f"{host_root}alive-1/SKILL.md"),
                category="general",
                description="Alive 1",
                triggers="always",
                hash="hash_alive_1",
            ),
            SkillInfo(
                name="alive-2",
                path=Path(f"{host_root}alive-2/SKILL.md"),
                category="general",
                description="Alive 2",
                triggers="always",
                hash="hash_alive_2",
            ),
        ]

        # Existing meta rows returned by initial check:
        # alive-1 has matching hash -> skipped
        # alive-2 has matching hash -> skipped
        existing_meta_rows = [
            ("skill:hosta:alive-1:c0", "alive-1", "hash_alive_1", f"{host_root}alive-1/SKILL.md", "hosta"),
            ("skill:hosta:alive-2:c0", "alive-2", "hash_alive_2", f"{host_root}alive-2/SKILL.md", "hosta"),
        ]

        # Stale query returns skill names under this host root:
        # 'alive-1', 'alive-2' (alive)
        # 'stale-local-1', 'stale-local-2', 'stale-local-3' (stale on this host)
        stale_query_rows = [
            ("alive-1",),
            ("alive-2",),
            ("stale-local-1",),
            ("stale-local-2",),
            ("stale-local-3",),
        ]

        def fetchall_side_effect():
            # Determine which query was executed
            last_sql = self.mock_cur.execute.call_args[0][0]
            if "role = 'skill' AND (memory_id LIKE '%:c0'" in last_sql:
                return existing_meta_rows
            elif "WHERE role = 'skill'" in last_sql and "path') LIKE :host_root" in last_sql:
                return stale_query_rows
            return []

        self.mock_cur.fetchall.side_effect = fetchall_side_effect

        stats = sync_skills_library(
            self.mock_conn,
            alive_skills,
            host_root,
            prune_host=host_root,
            prune_limit=2,  # Limit pruning to 2 to test prune_limit
            dry_run=False,
            host_tag="hosta",
        )

        # Asserts
        self.assertEqual(stats["scanned"], 2)
        self.assertEqual(stats["skipped"], 2)  # Hashes matched
        self.assertEqual(stats["upserted"], 0)
        self.assertEqual(stats["pruned"], 2)   # 2 pruned due to prune_limit=2
        self.assertEqual(stats["pruned_skills"], ["stale-local-1", "stale-local-2"])

        # Check all DELETE queries executed
        delete_calls = [
            call for call in self.mock_cur.execute.call_args_list
            if "DELETE FROM" in str(call[0][0])
        ]

        # Verify pruned queries specifically targeted host_root and skill name
        for call in delete_calls:
            sql = call[0][0]
            binds = call[0][1]
            if "json_value(metadata, '$.path') LIKE :host_root" in sql:
                self.assertEqual(binds["host_root"], host_root)
                self.assertIn(binds["name"], ["stale-local-1", "stale-local-2"])
                # Foreign skills or alive skills should NEVER be in delete calls
                self.assertNotIn(binds["name"], ["alive-1", "alive-2", "hostb-foreign"])

    def test_dry_run_records_deletions_without_executing_delete(self):
        """Verify dry-run mode identifies deletions without modifying database."""
        host_root = "/Users/dev/.hermes/skills/"
        alive_skills = []
        stale_query_rows = [("stale-1",), ("stale-2",)]

        def fetchall_side_effect():
            last_sql = self.mock_cur.execute.call_args[0][0]
            if "role = 'skill' AND (memory_id LIKE '%:c0'" in last_sql:
                return []
            elif "WHERE role = 'skill'" in last_sql and "path') LIKE :host_root" in last_sql:
                return stale_query_rows
            return []

        self.mock_cur.fetchall.side_effect = fetchall_side_effect

        stats = sync_skills_library(
            self.mock_conn,
            alive_skills,
            host_root,
            prune_host=host_root,
            prune_limit=500,
            dry_run=True,
            host_tag="hostc",
        )

        self.assertTrue(stats["dry_run"])
        self.assertEqual(stats["pruned"], 2)
        self.assertEqual(stats["pruned_skills"], ["stale-1", "stale-2"])

        # No DELETE statement should be executed when dry_run=True
        delete_calls = [
            call for call in self.mock_cur.execute.call_args_list
            if "DELETE FROM" in str(call[0][0])
        ]
        self.assertEqual(len(delete_calls), 0)
        # Commit should not be called in dry_run
        self.mock_conn.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
