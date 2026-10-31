import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

scripts_dir = os.path.join(root_dir, "scripts")
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

import tests.conftest
import __init__ as oracle_plugin
from scripts.sync_skills import (
    SkillInfo,
    chunk_markdown_text,
    discover_skills,
    extract_body_triggers,
    format_skill_payload,
    parse_frontmatter,
    parse_skill_file,
    safe_embed_text,
    safe_varchar_bytes,
    sync_skills_library,
)


class TestSkillSyncClamping(unittest.TestCase):
    def test_safe_varchar_bytes_ascii(self):
        text = "a" * 5000
        clamped = safe_varchar_bytes(text, max_bytes=4000)
        self.assertEqual(len(clamped.encode("utf-8")), 4000)

    def test_safe_varchar_bytes_multibyte_boundary(self):
        euro = "€"  # 3 bytes
        text = euro * 1334  # 4002 bytes
        clamped = safe_varchar_bytes(text, max_bytes=4000)
        encoded = clamped.encode("utf-8")
        self.assertLessEqual(len(encoded), 4000)
        self.assertEqual(len(encoded), 3999)
        self.assertEqual(clamped, euro * 1333)

    def test_safe_embed_text_sequence_clamp(self):
        text = "word " * 500
        clamped = safe_embed_text(text, max_chars=1200)
        self.assertLessEqual(len(clamped), 1200)

    def test_safe_varchar_and_embed_empty_none(self):
        self.assertEqual(safe_varchar_bytes(None), "")
        self.assertEqual(safe_varchar_bytes(""), "")
        self.assertEqual(safe_embed_text(None), "")
        self.assertEqual(safe_embed_text(""), "")


class TestFrontmatterAndTriggers(unittest.TestCase):
    def test_parse_frontmatter_standard(self):
        raw = """---
name: test-skill
description: "A test skill for unit testing."
version: 1.0.0
---
# Header
Body content here.
"""
        fm, body = parse_frontmatter(raw)
        self.assertEqual(fm.get("name"), "test-skill")
        self.assertEqual(fm.get("description"), "A test skill for unit testing.")
        self.assertIn("# Header", body)

    def test_parse_frontmatter_block_scalar_with_chomping(self):
        raw = """---
name: block-skill
description: >-
  This is a multiline
  folded description.
tags: [tag1, tag2]
metadata:
  hermes:
    tags: [h1, h2]
---
Body text.
"""
        fm, body = parse_frontmatter(raw)
        self.assertEqual(fm.get("name"), "block-skill")
        self.assertIn("multiline folded description", fm.get("description", ""))
        self.assertEqual(fm.get("tags"), ["tag1", "tag2"])
        if isinstance(fm.get("metadata"), dict) and "hermes" in fm["metadata"]:
            self.assertEqual(fm["metadata"]["hermes"]["tags"], ["h1", "h2"])

    def test_parse_frontmatter_no_fm(self):
        raw = "# Just Markdown\nNo frontmatter here."
        fm, body = parse_frontmatter(raw)
        self.assertEqual(fm, {})
        self.assertEqual(body, raw)

    def test_extract_body_triggers(self):
        body = """# Skill Title
Intro text.

## When to Use

- "Run an A/B test on pricing."
- When comparing two variants.
- 1. High conversion friction.

Don't use for: casual debugging.

## Other Section
Other text.
"""
        triggers = extract_body_triggers(body)
        self.assertIn("Run an A/B test on pricing.", triggers)
        self.assertIn("When comparing two variants.", triggers)
        self.assertIn("High conversion friction.", triggers)
        self.assertNotIn("Don't use for: casual debugging.", triggers)
        self.assertNotIn("Other text.", triggers)


class TestSkillDiscoveryAndFormatting(unittest.TestCase):
    def test_discover_skills_in_tempdir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            skill1_dir = tmppath / ".agents" / "skills" / "skill1"
            skill1_dir.mkdir(parents=True)
            (skill1_dir / "SKILL.md").write_text(
                "---\nname: skill-one\ndescription: Skill one description.\n---\nBody",
                encoding="utf-8",
            )

            arch_dir = tmppath / ".archive" / "old-skill"
            arch_dir.mkdir(parents=True)
            (arch_dir / "SKILL.md").write_text(
                "---\nname: old-skill\ndescription: Archived.\n---\nBody",
                encoding="utf-8",
            )

            skills = discover_skills(dirs=[skill1_dir.parent])
            names = [s.name for s in skills]
            self.assertIn("skill-one", names)
            self.assertNotIn("old-skill", names)

    def test_format_skill_payload_meta_mode(self):
        skill = SkillInfo(
            name="ab-testing",
            description="Run growth experiments.",
            triggers=["Split test pages"],
            tags=["Growth", "Testing"],
            category="marketing",
            path=Path("/tmp/ab-testing/SKILL.md"),
            content_hash="abc123hash",
        )
        rows = format_skill_payload(skill, mode="meta")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["memory_id"], "skill:ab-testing")
        self.assertEqual(row["role"], "skill")
        self.assertEqual(row["session_id"], "skills")
        self.assertEqual(row["target"], "memory")
        self.assertEqual(row["conv_ref"], "ab-testing")
        self.assertIn("[skill_ingest]", row["content"])
        self.assertIn("[skill:ab-testing]", row["content"])
        self.assertIn("When to use: Split test pages", row["content"])
        self.assertEqual(row["metadata"]["kind"], "skill_ingest")
        self.assertEqual(row["metadata"]["hash"], "abc123hash")

    def test_format_skill_payload_full_mode(self):
        skill = SkillInfo(
            name="full-skill",
            description="A detailed skill.",
            markdown_body="Word " * 200,
            path=Path("/tmp/full-skill/SKILL.md"),
            content_hash="hashxyz",
        )
        rows = format_skill_payload(skill, mode="full")
        self.assertGreater(len(rows), 1)
        self.assertEqual(rows[0]["memory_id"], "skill:full-skill:c0")
        self.assertEqual(rows[1]["memory_id"], "skill:full-skill:c1")


class TestSyncSkillsLibrary(unittest.TestCase):
    def test_sync_skills_dry_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            s_dir = Path(tmpdir) / "demo"
            s_dir.mkdir()
            (s_dir / "SKILL.md").write_text("---\nname: demo\ndescription: Demo.\n---\n", encoding="utf-8")
            res = sync_skills_library(provider=None, mode="meta", dry_run=True, dirs=[tmpdir])
            self.assertEqual(res["status"], "dry_run")
            self.assertEqual(res["scanned"], 1)

    def test_sync_skills_skips_unchanged(self):
        provider = MagicMock()
        provider._pool = MagicMock()
        provider._table = "HERMES_AGENT_MEMORY"

        conn = MagicMock()
        cur = MagicMock()
        provider._pool.acquire.return_value.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur

        with tempfile.TemporaryDirectory() as tmpdir:
            s_dir = Path(tmpdir) / "demo"
            s_dir.mkdir()
            (s_dir / "SKILL.md").write_text("---\nname: demo\ndescription: Demo.\n---\n", encoding="utf-8")
            skill = parse_skill_file(s_dir / "SKILL.md")

            cur.fetchall.return_value = [
                ("skill:demo", json.dumps({"hash": skill.content_hash, "skill_name": "demo"}))
            ]

            res = sync_skills_library(provider=provider, mode="meta", force=False, dirs=[tmpdir])
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["scanned"], 1)
            self.assertEqual(res["skipped"], 1)
            self.assertEqual(res["indexed"], 0)
            self.assertEqual(provider._insert_many.call_count, 0)

    def test_sync_skills_updates_changed(self):
        provider = MagicMock()
        provider._pool = MagicMock()
        provider._table = "HERMES_AGENT_MEMORY"

        conn = MagicMock()
        cur = MagicMock()
        provider._pool.acquire.return_value.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur

        with tempfile.TemporaryDirectory() as tmpdir:
            s_dir = Path(tmpdir) / "demo"
            s_dir.mkdir()
            (s_dir / "SKILL.md").write_text("---\nname: demo\ndescription: Demo.\n---\n", encoding="utf-8")

            cur.fetchall.return_value = [
                ("skill:demo", json.dumps({"hash": "stale_hash", "skill_name": "demo"}))
            ]

            res = sync_skills_library(provider=provider, mode="meta", force=False, dirs=[tmpdir])
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["scanned"], 1)
            self.assertEqual(res["updated"], 1)
            self.assertEqual(provider._insert_many.call_count, 1)


class TestProviderIntegration(unittest.TestCase):
    def test_provider_sync_skills_schema(self):
        provider = oracle_plugin.OracleMemoryProvider()
        schemas = {s["name"]: s for s in provider.get_tool_schemas()}
        self.assertIn("oracle_sync_skills", schemas)
        props = schemas["oracle_sync_skills"]["parameters"]["properties"]
        self.assertIn("mode", props)
        self.assertIn("force", props)
        self.assertIn("dirs", props)

    def test_provider_handle_tool_call_uninitialized(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = None
        res = provider.handle_tool_call("oracle_sync_skills", {})
        # tool_error returns error string or error dictionary
        self.assertTrue("error" in str(res).lower())

    @patch("scripts.sync_skills.sync_skills_library")
    def test_provider_handle_tool_call_dispatched(self, mock_sync):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        mock_sync.return_value = {"status": "success", "indexed": 5}

        res = json.loads(provider.handle_tool_call("oracle_sync_skills", {"mode": "meta", "force": True}))
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["indexed"], 5)
        mock_sync.assert_called_once()

    def test_provider_sync_skills_fallback_loader(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        provider._insert_many = MagicMock()
        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "test-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: fallback-test\ndescription: Test fallback loader\n---\nBody",
                encoding="utf-8",
            )
            orig_import = __import__
            def mock_import(name, *args, **kwargs):
                if "scripts" in name:
                    raise ImportError("No module named scripts")
                return orig_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=mock_import):
                res = provider.sync_skills(mode="meta", dirs=[td])
                self.assertEqual(res["status"], "success")
                self.assertEqual(res["scanned"], 1)


if __name__ == "__main__":
    unittest.main()
