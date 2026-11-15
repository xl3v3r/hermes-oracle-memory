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


def _stale_query(sql: str) -> bool:
    return "SELECT DISTINCT json_value(metadata, '$.skill_name')" in sql and "memory_id LIKE :own" in sql


class TestSyncPrune(unittest.TestCase):
    """Pruning (1.1.1.4, #5) is host-scoped: only rows with this host's own id
    prefix whose SKILL.md sat under a pruned root and is no longer found."""

    def setUp(self):
        self.mock_conn = MagicMock()
        self.mock_cur = MagicMock()
        self.mock_conn.cursor.return_value.__enter__.return_value = self.mock_cur

    def _existing(self, rows):
        def fetchall_side_effect():
            last_sql = self.mock_cur.execute.call_args[0][0]
            if "role = 'skill' AND (memory_id LIKE '%:c0'" in last_sql:
                return rows["existing"]
            if _stale_query(last_sql):
                return rows["stale"]
            return []

        self.mock_cur.fetchall.side_effect = fetchall_side_effect

    def test_prune_scenario_table(self):
        """1. Alive local skills: never pruned.
        2. This host's stale skills under the root: deleted up to prune_limit.
        3. Other hosts' copies and host-less legacy rows: never matched (the
           stale query and every DELETE are bound to this host's id prefix)."""
        host_root = "/srv/fixtures/.hermes/skills/"
        alive_skills = [
            SkillInfo(name=n, path=Path(f"{host_root}{n}/SKILL.md"), category="general",
                      description=n, triggers="always", hash=f"hash_{n}")
            for n in ("alive-1", "alive-2")
        ]
        self._existing({
            "existing": [
                ("skill:hosta:alive-1:c0", "alive-1", "hash_alive-1", f"{host_root}alive-1/SKILL.md", "hosta"),
                ("skill:hosta:alive-2:c0", "alive-2", "hash_alive-2", f"{host_root}alive-2/SKILL.md", "hosta"),
            ],
            "stale": [("alive-1",), ("alive-2",), ("stale-local-1",), ("stale-local-2",), ("stale-local-3",)],
        })

        stats = sync_skills_library(
            self.mock_conn, alive_skills, host_root,
            prune_host=host_root, prune_limit=2, dry_run=False, host_tag="hosta",
        )

        self.assertEqual(stats["scanned"], 2)
        self.assertEqual(stats["skipped"], 2)
        self.assertEqual(stats["upserted"], 0)
        self.assertEqual(stats["prune_candidates"], 3)
        self.assertEqual(stats["pruned"], 2)
        self.assertEqual(stats["pruned_skills"], ["stale-local-1", "stale-local-2"])

        stale_calls = [c for c in self.mock_cur.execute.call_args_list if _stale_query(c[0][0])]
        self.assertEqual(len(stale_calls), 1)
        sql, binds = stale_calls[0][0]
        self.assertEqual(binds["own"], "skill:hosta:%")
        self.assertEqual(binds["r0"], "/srv/fixtures/.hermes/skills/%")
        self.assertIn("json_value(metadata, '$.path') LIKE :r0 ESCAPE", sql)

        delete_calls = [c for c in self.mock_cur.execute.call_args_list if "DELETE" in str(c[0][0])]
        self.assertEqual(
            [c[0][1] for c in delete_calls],
            [{"pfx": "skill:hosta:stale-local-1:c%"}, {"pfx": "skill:hosta:stale-local-2:c%"}],
        )
        for c in delete_calls:
            self.assertIn("memory_id LIKE :pfx ESCAPE", c[0][0])
            self.assertNotIn("conv_ref", c[0][0])
        self.mock_conn.commit.assert_called_once()

    def test_dry_run_records_deletions_without_executing_delete(self):
        host_root = "/Users/dev/.hermes/skills/"
        self._existing({"existing": [], "stale": [("stale-1",), ("stale-2",)]})

        stats = sync_skills_library(
            self.mock_conn, [], host_root,
            prune_host=host_root, prune_limit=500, dry_run=True, host_tag="hostc",
        )

        self.assertTrue(stats["dry_run"])
        self.assertEqual(stats["pruned"], 2)
        self.assertEqual(stats["pruned_skills"], ["stale-1", "stale-2"])
        delete_calls = [c for c in self.mock_cur.execute.call_args_list if "DELETE" in str(c[0][0])]
        self.assertEqual(len(delete_calls), 0)
        self.mock_conn.commit.assert_not_called()

    def test_no_prune_unless_asked(self):
        self._existing({"existing": [], "stale": [("stale-1",)]})
        stats = sync_skills_library(self.mock_conn, [], "/srv/fixtures/.hermes/skills/", host_tag="hosta")
        self.assertEqual(stats["pruned"], 0)
        self.assertFalse(any(_stale_query(c[0][0]) for c in self.mock_cur.execute.call_args_list))

    def test_like_wildcards_in_host_and_root_are_escaped(self):
        self._existing({"existing": [], "stale": []})
        sync_skills_library(self.mock_conn, [], "/srv/my_skills%/", prune_host="/srv/my_skills%/", host_tag="host_a")
        sql, binds = next(c[0] for c in self.mock_cur.execute.call_args_list if _stale_query(c[0][0]))
        self.assertEqual(binds["own"], "skill:host\\_a:%")
        self.assertEqual(binds["r0"], "/srv/my\\_skills\\%/%")


if __name__ == "__main__":
    unittest.main()
