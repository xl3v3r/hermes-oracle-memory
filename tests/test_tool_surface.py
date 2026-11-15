"""1.1.1.4 tool-surface hardening (#11): batch caps, all-or-nothing batch
insert, search output size and tags, schema gaps, oracle_chunk validation,
built-in memory mirroring for replace/remove, skill rows read-only, and
case-insensitive tag matching. Fully mocked; no database."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401  (stubs agent.* / tools.*)
import __init__ as oracle_plugin  # noqa: E402


def _provider(rowcount=1):
    p = oracle_plugin.OracleMemoryProvider()
    p._table = "HERMES_AGENT_MEMORY"
    p._onnx_model = "BGE_BASE_EN_V1_5"
    p._session_id = "sess-1"
    conn, cur = MagicMock(), MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    cur.rowcount = rowcount
    p._pool = MagicMock()
    p._pool.acquire.return_value = conn
    return p, conn, cur


class TestOracleAdd(unittest.TestCase):
    def test_batch_cap(self):
        p, _, _ = _provider()
        p._insert_many = MagicMock()
        out = p.handle_tool_call("oracle_add", {"facts": [f"f{i}" for i in range(oracle_plugin._ADD_MAX_FACTS + 1)]})
        self.assertIn("too many facts", out)
        p._insert_many.assert_not_called()

    def test_fact_size_cap(self):
        p, _, _ = _provider()
        p._insert = MagicMock()
        out = p.handle_tool_call("oracle_add", {"content": "x" * (oracle_plugin._MAX_FACT_CHARS + 1)})
        self.assertIn("oracle_chunk", out)
        p._insert.assert_not_called()

    def test_batch_failure_stores_nothing_and_reports(self):
        p, _, _ = _provider()
        p._insert_many = MagicMock(side_effect=RuntimeError("ORA-12899: value too large"))
        out = p.handle_tool_call("oracle_add", {"facts": ["a", "b"]})
        self.assertIn("ORA-12899", out)
        self.assertNotIn("memory_ids", out)

    def test_skill_prefix_reserved(self):
        p, _, cur = _provider()
        out = p.handle_tool_call("oracle_add", {"content": "x", "memory_id": "skill:foo:c0"})
        self.assertIn("reserved", out)
        cur.execute.assert_not_called()


class TestOracleUpdate(unittest.TestCase):
    def test_skill_id_refused(self):
        p, _, cur = _provider()
        self.assertIn("SKILL.md", p.handle_tool_call("oracle_update", {"memory_id": "skill:x:c0", "content": "y"}))
        cur.execute.assert_not_called()

    def test_skill_row_with_other_id_refused(self):
        p, _, cur = _provider(rowcount=0)
        cur.fetchone.return_value = (1,)
        self.assertIn("SKILL.md", p.handle_tool_call("oracle_update", {"memory_id": "legacy-skill", "tags": ["a"]}))

    def test_update_sql_excludes_skill_rows(self):
        p, _, cur = _provider()
        p._update_row("m1", "new text")
        self.assertIn("NVL(role, 'fact') <> 'skill'", cur.execute.call_args[0][0])
        p._update_row("m1", None, tags=["a"])
        self.assertIn("NVL(role, 'fact') <> 'skill'", cur.execute.call_args[0][0])


class TestSearchPayload(unittest.TestCase):
    def test_content_cut_and_flagged(self):
        big = "y" * (oracle_plugin._SEARCH_CONTENT_MAX_CHARS + 10)
        out = oracle_plugin._search_tool_payload([
            {"memory_id": "a", "content": big, "dist": 0.1, "score": 0.5},
            {"memory_id": "b", "content": "short", "score": 0.4, "tags": ["t"]},
        ])
        a, b = out["results"]
        self.assertEqual(len(a["content"]), oracle_plugin._SEARCH_CONTENT_MAX_CHARS)
        self.assertTrue(a["content_truncated"])
        self.assertEqual(a["content_chars"], len(big))
        self.assertNotIn("dist", a)
        self.assertNotIn("content_truncated", b)
        self.assertEqual(b["tags"], ["t"])
        self.assertIn("oracle_get", out["note"])

    def test_fetch_rows_maps_tags(self):
        p, _, _ = _provider()
        cur = MagicMock()
        cur.fetchall.return_value = [("m1", "s", "a", "fact", "text", "memory", 0.0, 0.1, None, '["handoff","x"]'),
                                     ("m2", "s", "a", "fact", "text", "memory", 0.0, 0.1, None, None)]
        rows = p._fetch_rows(cur, "SELECT", {"k": 2})
        self.assertEqual(rows[0]["tags"], ["handoff", "x"])
        self.assertNotIn("tags", rows[1])

    def test_search_sql_projects_tags(self):
        p, _, _ = _provider()
        for sql, _ in (p._build_text_search_sql("{q}", 5, "", "any"),
                       p._build_vector_search_sql("q", None, 5, "", "any"),
                       p._build_hybrid_search_sql("q", None, "{q}", 5, "", "any")):
            self.assertIn("'$.tags' RETURNING VARCHAR2(4000)", sql)


class TestTagsCaseInsensitive(unittest.TestCase):
    def test_filter_lowercases_stored_tags(self):
        sql, binds = oracle_plugin._tag_filter_sql(["Handoff"])
        self.assertIn("@.lower() == $tg0", sql)
        self.assertEqual(binds, {"tg0": "handoff"})


class TestSchema(unittest.TestCase):
    def test_load_skill_schema_has_path_and_variant(self):
        schema = next(s for s in oracle_plugin.TOOL_SCHEMAS if s["name"] == "oracle_load_skill")
        props = schema["parameters"]["properties"]
        self.assertEqual(props["path"]["type"], "string")
        self.assertEqual(props["variant"]["type"], "integer")


class TestChunkParams(unittest.TestCase):
    def test_defaults_and_bounds(self):
        f = oracle_plugin._chunk_params
        self.assertEqual(f(None, None), (80, 10))
        self.assertEqual(f(100, 0), (100, 0))  # 0 is kept, not replaced by the default
        for mw, ov in ((0, 0), (-5, 0), (9, 0), (1001, 0), (100, 51), (100, -1), ("x", 1)):
            with self.assertRaises(ValueError, msg=(mw, ov)):
                f(mw, ov)

    def test_tool_rejects_bad_sizes_before_any_work(self):
        p, _, cur = _provider()
        out = p.handle_tool_call("oracle_chunk", {"content": "a b c", "max_words": 0})
        self.assertIn("max_words", out)
        cur.execute.assert_not_called()

    def test_local_split_has_no_empty_or_redundant_chunks(self):
        p = oracle_plugin.OracleMemoryProvider()  # no pool: local split
        text = " ".join(f"w{i}" for i in range(25))
        chunks = p._chunk_text(text, 10, 5)
        self.assertTrue(all(chunks))
        self.assertEqual(chunks[-1].split()[-1], "w24")
        self.assertEqual(len(chunks), 4)  # 0-9, 5-14, 10-19, 15-24


class TestMemoryMirror(unittest.TestCase):
    def setUp(self):
        self.p, self.conn, self.cur = _provider()
        self.p._insert = MagicMock()

    def test_add(self):
        self.p.on_memory_write("add", "user", "likes tea", {"tool_name": "memory"})
        kw = self.p._insert.call_args.kwargs
        self.assertEqual(kw["content"], "likes tea")
        self.assertEqual(kw["target"], "user")
        self.assertEqual(kw["metadata"]["kind"], "builtin")
        self.assertEqual(kw["metadata"]["host"], oracle_plugin.get_host_id())
        self.cur.execute.assert_not_called()

    def test_replace_inserts_new_then_deletes_previous(self):
        order = []
        self.p._insert.side_effect = lambda **kw: order.append("insert")
        self.cur.execute.side_effect = lambda *a, **k: order.append("delete")
        self.p.on_memory_write("replace", "memory", "new entry", {"previous_content": "old entry"})
        self.assertEqual(order, ["insert", "delete"])
        self.assertNotIn("previous_content", self.p._insert.call_args.kwargs["metadata"])
        sql, binds = self.cur.execute.call_args[0]
        self.assertIn("DBMS_LOB.COMPARE(content, TO_CLOB(:c)) = 0", sql)
        self.assertIn("LIKE 'md\\_%' ESCAPE '\\'", sql)
        self.assertEqual(binds["c"], "old entry")
        self.assertEqual(binds["tgt"], "memory")
        self.conn.commit.assert_called_once()

    def test_remove_deletes_only(self):
        self.p.on_memory_write("remove", "user", "", {"previous_content": "gone"})
        self.p._insert.assert_not_called()
        self.assertEqual(self.cur.execute.call_args[0][1]["c"], "gone")

    def test_without_previous_content_nothing_is_deleted(self):
        self.p.on_memory_write("remove", "memory", "", {"old_text": "gone"})
        self.p.on_memory_write("replace", "memory", "new", {})
        self.cur.execute.assert_not_called()
        self.p._insert.assert_called_once()  # the replacement itself is still mirrored

    def test_failed_insert_keeps_previous_mirror(self):
        self.p._insert.side_effect = RuntimeError("ORA-03113")
        self.p.on_memory_write("replace", "memory", "new", {"previous_content": "old"})
        self.cur.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
