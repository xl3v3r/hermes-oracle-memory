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
        # Return 501 rows to indicate more is True
        names_page = [(f"skill-{i:03d}", "cat") for i in range(501)]
        desc_rows = [(f"skill-{i:03d}", f"[skill_ingest] [skill:skill-{i:03d}] skill-{i:03d} — Description {i}\nWhen to use: always") for i in range(500)]

        # Mock execute side effects: first call returns names, follow-up calls return descriptions
        def mock_fetchall():
            call_sql = self.mock_cursor.execute.call_args[0][0]
            if "DISTINCT" in call_sql:
                return names_page
            return desc_rows

        self.mock_cursor.fetchall.side_effect = mock_fetchall

        raw_res = self.provider.handle_tool_call("oracle_load_skill", {"action": "list", "offset": 100})
        res = json.loads(raw_res)

        self.assertEqual(res["offset"], 100)
        self.assertEqual(res["count_in_page"], 500)
        self.assertTrue(res["more"])
        self.assertEqual(len(res["skills"]), 500)
        self.assertEqual(res["skills"][0]["name"], "skill-000")
        self.assertEqual(res["skills"][0]["description"], "Description 0")

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
