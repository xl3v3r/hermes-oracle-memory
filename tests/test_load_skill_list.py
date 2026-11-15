import json
import os
import sys
import unittest
from unittest.mock import MagicMock

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import tests.conftest
import __init__ as oracle_plugin


class TestLoadSkillList(unittest.TestCase):
    def setUp(self):
        self.provider = oracle_plugin.OracleMemoryProvider()
        self.provider._pool = MagicMock()
        self.mock_conn = MagicMock()
        self.mock_cursor = MagicMock()
        self.provider._pool.acquire.return_value.__enter__.return_value = self.mock_conn
        self.mock_conn.cursor.return_value.__enter__.return_value = self.mock_cursor

    def test_list_pagination_and_more(self):
        """Verify pagination offset, page size limit, and 'more' flag calculation."""
        # 501 rows (one per distinct name) -> more is True
        rows = [
            (f"skill-{i:03d}", "cat", f"[skill_ingest] [skill:skill-{i:03d}] skill-{i:03d} — Description {i}\nWhen to use: always", 1)
            for i in range(501)
        ]
        self.mock_cursor.fetchall.return_value = rows

        raw_res = self.provider.handle_tool_call("oracle_load_skill", {"action": "list", "offset": 100})
        res = json.loads(raw_res)

        self.assertEqual(res["offset"], 100)
        self.assertEqual(res["count_in_page"], 500)
        self.assertTrue(res["more"])
        self.assertEqual(len(res["skills"]), 500)
        self.assertEqual(res["skills"][0]["name"], "skill-000")
        self.assertEqual(res["skills"][0]["description"], "Description 0")
        sql, binds = self.mock_cursor.execute.call_args[0][0], self.mock_cursor.execute.call_args[0][1]
        self.assertEqual(binds["o"], 100)
        self.assertEqual(binds["limit"], 501)
        # Paging is over distinct names (one row per name), so pages never repeat a name.
        self.assertIn("PARTITION BY json_value(metadata, '$.skill_name')", sql)
        self.assertIn("WHERE rn = 1", sql)

    def test_list_reports_copies(self):
        self.mock_cursor.fetchall.return_value = [
            ("dup", "cat", "[skill_ingest] [skill:dup] dup — Two copies", 2),
            ("one", "cat", "[skill_ingest] [skill:one] one — One copy", 1),
        ]
        res = json.loads(self.provider.handle_tool_call("oracle_load_skill", {"action": "list"}))
        self.assertEqual(res["skills"][0]["copies"], 2)
        self.assertNotIn("copies", res["skills"][1])

    def test_description_regex_extraction(self):
        """Verify extraction of description after em-dash/hyphen, excluding 'When to use:'."""
        test_cases = [
            (
                "[skill_ingest] [skill:alpha] alpha — Standardize address formats\nWhen to use: address cleanup",
                "Standardize address formats",
            ),
            (
                "[skill_ingest] [skill:beta] beta - Convert markdown to HTML tables",
                "Convert markdown to HTML tables",
            ),
            (
                "[skill_ingest] [skill:gamma] gamma Simple plain description line\nWhen to use: testing",
                "Simple plain description line",
            ),
            (
                "[skill_ingest] [skill:usps-address-validator] usps-address-validator — Validate and standardize US postal addresses\nWhen to use: address cleanup",
                "Validate and standardize US postal addresses",
            ),
        ]
        rows = [
            ("alpha", "tools", test_cases[0][0]),
            ("beta", "tools", test_cases[1][0]),
            ("gamma", "tools", test_cases[2][0]),
            ("usps-address-validator", "postal", test_cases[3][0]),
        ]
        self.mock_cursor.fetchall.return_value = rows

        raw_res = self.provider.handle_tool_call("oracle_load_skill", {"action": "list", "filter": "tools"})
        res = json.loads(raw_res)

        self.assertEqual(res["count_in_page"], 4)
        self.assertEqual(res["skills"][0]["description"], "Standardize address formats")
        self.assertEqual(res["skills"][1]["description"], "Convert markdown to HTML tables")
        self.assertEqual(res["skills"][2]["description"], "Simple plain description line")
        self.assertEqual(res["skills"][3]["description"], "Validate and standardize US postal addresses")

    def test_filter_applies_sql_filter_without_full_scan(self):
        """Verify filter parameter passes SQL LIKE bind and avoids pulling full corpus."""
        self.mock_cursor.fetchall.return_value = [
            ("apple-notes", "apple", "[skill_ingest] [skill:apple-notes] apple-notes — Export notes\nWhen to use: export")
        ]

        raw_res = self.provider.handle_tool_call("oracle_load_skill", {"action": "list", "filter": "apple"})
        res = json.loads(raw_res)

        # Check executed query contained SQL filter
        call_args = self.mock_cursor.execute.call_args
        sql, binds = call_args[0][0], call_args[0][1]
        self.assertIn("LIKE UPPER(:f)", sql)
        self.assertEqual(binds["f"], "%apple%")
        self.assertEqual(res["count_in_page"], 1)
        self.assertEqual(res["skills"][0]["name"], "apple-notes")


if __name__ == "__main__":
    unittest.main()
