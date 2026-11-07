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


class TestLoadSkillFetch(unittest.TestCase):
    def setUp(self):
        self.provider = oracle_plugin.OracleMemoryProvider()
        self.provider._pool = MagicMock()
        self.mock_conn = MagicMock()
        self.mock_cursor = MagicMock()
        self.provider._pool.acquire.return_value.__enter__.return_value = self.mock_conn
        self.mock_conn.cursor.return_value.__enter__.return_value = self.mock_cursor

    def test_name_injection_rejected(self):
        """Verify strict regex validation on skill name rejects SQL injection attempts."""
        injections = [
            "skill;a DROP TABLE users",
            "skill' OR 1=1 --",
            "skill/../../etc/passwd",
            "skill$(whoami)",
            "bad name with spaces",
        ]
        for bad_name in injections:
            res = self.provider.handle_tool_call("oracle_load_skill", {"action": "fetch", "skill": bad_name})
            self.assertIn("error", res.lower())

    def test_not_in_corpus_error(self):
        """Verify proper error when skill is not found in corpus."""
        self.mock_cursor.fetchall.return_value = []
        res = self.provider.handle_tool_call("oracle_load_skill", {"action": "fetch", "skill": "nonexistent-skill"})
        self.assertIn("error", res.lower())
        self.assertIn("not in corpus", res.lower())

    def test_fetch_full_skill_json_shape(self):
        """Verify JSON shape, body reconstruction, and reference files on happy path."""
        meta_json = json.dumps({
            "skill_name": "test-skill",
            "category": "utilities",
            "path": "/srv/fixtures/.hermes/skills/test-skill/SKILL.md",
            "hash": "abcdef123456",
            "mtime": "2026-11-06T12:00:00Z",
            "total_chunks": 3,
        })
        ref_meta_json = json.dumps({
            "skill_name": "test-skill",
            "category": "utilities",
            "path": "/srv/fixtures/.hermes/skills/test-skill/SKILL.md",
            "hash": "abcdef123456",
            "mtime": "2026-11-06T12:00:00Z",
            "reference_file": "references/api.md",
            "total_chunks": 3,
        })

        # Columns: idx, content, ref, total, metadata, memory_id
        fixture_rows = [
            (
                0,
                "[skill_ingest] [skill:test-skill] test-skill — Useful utility skill\nWhen to use: always\nTags: test",
                None,
                3,
                meta_json,
                "skill:hosta:test-skill:c0",
            ),
            (
                1,
                "[skill_ingest] [skill:test-skill] test-skill (chunk 1/1):\nDetailed instructions on how to use test skill.",
                None,
                3,
                meta_json,
                "skill:hosta:test-skill:c1",
            ),
            (
                2,
                "[skill_ingest] [skill:test-skill] test-skill (reference: references/api.md) (chunk 1/1):\nAPI reference details and parameters.",
                "references/api.md",
                3,
                ref_meta_json,
                "skill:hosta:test-skill:c2",
            ),
        ]
        self.mock_cursor.fetchall.return_value = fixture_rows

        raw_res = self.provider.handle_tool_call("oracle_load_skill", {"action": "fetch", "skill": "test-skill"})
        res = json.loads(raw_res)

        # Assert shape
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["skill"], "test-skill")
        self.assertEqual(res["category"], "utilities")
        self.assertEqual(res["source_host_path"], "/srv/fixtures/.hermes/skills/test-skill/SKILL.md")
        self.assertEqual(res["content_hash"], "abcdef123456")
        self.assertEqual(res["mtime"], "2026-11-06T12:00:00Z")
        self.assertEqual(res["chunk_count"], 2)
        self.assertIn("Detailed instructions on how to use test skill.", res["markdown"])
        self.assertIn("references/api.md", res["references"])
        self.assertEqual(res["references"]["references/api.md"], "API reference details and parameters.")
        self.assertGreater(res["word_count"], 0)

    def test_legacy_meta_only_row(self):
        """Verify backward compatibility: legacy single row (idx IS NULL) degrades to status meta_only."""
        meta_json = json.dumps({
            "skill_name": "legacy-skill",
            "category": "legacy",
            "path": "/srv/fixtures/.hermes/skills/legacy-skill/SKILL.md",
            "hash": "legacyhash999",
            "mtime": "2026-10-01T00:00:00Z",
        })
        fixture_rows = [
            (
                None,
                "[skill_ingest] [skill:legacy-skill] legacy-skill — Legacy meta description",
                None,
                1,
                meta_json,
                "skill:legacy-skill",
            )
        ]
        self.mock_cursor.fetchall.return_value = fixture_rows

        raw_res = self.provider.handle_tool_call("oracle_load_skill", {"action": "fetch", "skill": "legacy-skill"})
        res = json.loads(raw_res)
        self.assertEqual(res["status"], "meta_only")
        self.assertEqual(res["skill"], "legacy-skill")
        self.assertEqual(res["category"], "legacy")
        self.assertIn("Legacy meta description", res["markdown"])
        self.assertEqual(res["references"], {})

    def test_truncation_cap(self):
        """Verify truncation status when retrieved chunks exceed max chunk threshold."""
        meta_json = json.dumps({
            "skill_name": "huge-skill",
            "category": "massive",
            "path": "/srv/fixtures/.hermes/skills/huge-skill/SKILL.md",
        })
        # Simulate 450 chunks (exceeding default 400 cap)
        fixture_rows = [
            (
                0,
                "[skill_ingest] [skill:huge-skill] huge-skill — Giant skill",
                None,
                451,
                meta_json,
                "skill:hosta:huge-skill:c0",
            )
        ]
        for i in range(1, 451):
            fixture_rows.append((
                i,
                f"[skill_ingest] [skill:huge-skill] huge-skill (chunk {i}/450):\nWord chunk {i} content text",
                None,
                451,
                meta_json,
                f"skill:hosta:huge-skill:c{i}",
            ))
        self.mock_cursor.fetchall.return_value = fixture_rows

        raw_res = self.provider.handle_tool_call("oracle_load_skill", {"action": "fetch", "skill": "huge-skill"})
        res = json.loads(raw_res)
        self.assertEqual(res["status"], "truncated")
        self.assertEqual(res["skill"], "huge-skill")

    def test_multi_variant_resolution(self):
        """Verify cross-host path collision returns multiple_variants manifest and disambiguates."""
        meta_1 = json.dumps({
            "skill_name": "findmy",
            "category": "apple",
            "path": "/srv/fixtures/.hermes/skills/apple/findmy/SKILL.md",
            "hash": "hash_hostb",
            "mtime": "2026-11-05T00:00:00Z",
            "host": "hostb",
        })
        meta_2 = json.dumps({
            "skill_name": "findmy",
            "category": "apple",
            "path": "/Users/dev/.hermes/skills/example/sample-skill/SKILL.md",
            "hash": "hash_hostc",
            "mtime": "2026-11-06T00:00:00Z",
            "host": "hostc",
        })

        fixture_rows = [
            (0, "[skill_ingest] [skill:findmy] findmy — UserB findmy", None, 1, meta_1, "skill:hostb:findmy:c0"),
            (1, "[skill_ingest] [skill:findmy] findmy (chunk 1/1):\nUserB content", None, 1, meta_1, "skill:hostb:findmy:c1"),
            (0, "[skill_ingest] [skill:findmy] findmy — UserC findmy", None, 1, meta_2, "skill:hostc:findmy:c0"),
            (1, "[skill_ingest] [skill:findmy] findmy (chunk 1/1):\nUserC content", None, 1, meta_2, "skill:hostc:findmy:c1"),
        ]
        self.mock_cursor.fetchall.return_value = fixture_rows

        # Without disambiguation -> returns manifest
        raw_res = self.provider.handle_tool_call("oracle_load_skill", {"action": "fetch", "skill": "findmy"})
        res = json.loads(raw_res)
        self.assertEqual(res["status"], "multiple_variants")
        self.assertEqual(len(res["variants"]), 2)

        # Disambiguating by variant index 0
        raw_res = self.provider.handle_tool_call("oracle_load_skill", {"action": "fetch", "skill": "findmy", "variant": 0})
        res = json.loads(raw_res)
        self.assertEqual(res["status"], "ok")
        self.assertIn("UserB content", res["markdown"])

        # Disambiguating by explicit path
        raw_res = self.provider.handle_tool_call(
            "oracle_load_skill",
            {"action": "fetch", "skill": "findmy", "path": "/Users/dev/.hermes/skills/example/sample-skill/SKILL.md"},
        )
        res = json.loads(raw_res)
        self.assertEqual(res["status"], "ok")
        self.assertIn("UserC content", res["markdown"])

        # Invalid path returns error
        err_path_res = self.provider.handle_tool_call(
            "oracle_load_skill",
            {"action": "fetch", "skill": "findmy", "path": "/nonexistent/invalid/path.md"},
        )
        self.assertIn("error", err_path_res.lower())

        # Out-of-range variant index returns error
        err_var_res = self.provider.handle_tool_call(
            "oracle_load_skill",
            {"action": "fetch", "skill": "findmy", "variant": 99},
        )
        self.assertIn("error", err_var_res.lower())

    def test_legacy_meta_only_skill_ending_in_digit(self):
        """Verify skill ending in a number (like oauth2) is properly recognized as legacy meta_only, not body chunk."""
        meta_json = json.dumps({
            "skill_name": "oauth2",
            "category": "auth",
            "path": "/srv/fixtures/.hermes/skills/oauth2/SKILL.md",
            "hash": "oauthhash123",
            "mtime": "2026-11-06T00:00:00Z",
        })
        # SQL regexp_substr returns 2 for 'skill:oauth2' because '2' is trailing digits
        fixture_rows = [
            (
                2,
                "[skill_ingest] [skill:oauth2] oauth2 — OAuth2 authentication helper",
                None,
                1,
                meta_json,
                "skill:oauth2",
            )
        ]
        self.mock_cursor.fetchall.return_value = fixture_rows

        raw_res = self.provider.handle_tool_call("oracle_load_skill", {"action": "fetch", "skill": "oauth2"})
        res = json.loads(raw_res)
        self.assertEqual(res["status"], "meta_only")
        self.assertEqual(res["skill"], "oauth2")
        self.assertEqual(res["category"], "auth")
        self.assertIn("OAuth2 authentication helper", res["markdown"])


if __name__ == "__main__":
    unittest.main()
