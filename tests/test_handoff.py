"""Handoff rows (1.1.1.3): caller-chosen memory_id (upsert) and metadata.tags,
plus the exact-read oracle_get tool. All DB access is mocked."""
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401  (stubs agent.* / tools.*)
import __init__ as oracle_plugin  # noqa: E402


def _provider(rowcount=1, fetch=None):
    p = oracle_plugin.OracleMemoryProvider()
    p._table = "HERMES_AGENT_MEMORY"
    p._onnx_model = "BGE_BASE_EN_V1_5"
    p._session_id = "sess-1"
    conn, cur = MagicMock(), MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    cur.rowcount = rowcount
    cur.fetchall.return_value = fetch or []
    p._pool = MagicMock()
    p._pool.acquire.return_value = conn
    return p, conn, cur


class TestNormalizers(unittest.TestCase):
    def test_tags_list_string_json_and_csv(self):
        n = oracle_plugin._normalize_tags
        self.assertEqual(n(["Handoff", " project:X ", "handoff"]), ["handoff", "project:x"])
        self.assertEqual(n("handoff, project:x"), ["handoff", "project:x"])
        self.assertEqual(n('["a","B"]'), ["a", "b"])
        self.assertEqual(n(None), [])
        self.assertEqual(n(""), [])
        self.assertEqual(n(["", None, "  "]), [])
        self.assertEqual(n(["two  words"]), ["two words"])

    def test_tags_rejects_bad_values(self):
        n = oracle_plugin._normalize_tags
        with self.assertRaises(ValueError):
            n(["x" * 65])
        with self.assertRaises(ValueError):
            n(["bad\x00tag"])
        with self.assertRaises(ValueError):
            n([f"t{i}" for i in range(33)])
        with self.assertRaises(ValueError):
            n(42)

    def test_memory_id(self):
        m = oracle_plugin._normalize_memory_id
        for ok in ("handoff:oracle-plugin:release", "fact_abc123", "wal_20261017_user_lang", "a", "user@host/x.y"):
            self.assertEqual(m(ok), ok)
        self.assertEqual(m("  padded  "), "padded")
        for bad in ("", "   ", None, 7, ":leading", "has space", "x" * 129, "semi;colon", "quote'"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                m(bad)

    def test_tag_filter_is_fully_bound(self):
        sql, binds = oracle_plugin._tag_filter_sql(["Handoff", "x' OR 1=1 --"], "all")
        # wrapped in JSON_QUERY so the async JSON search index is never used
        self.assertIn("JSON_EXISTS(JSON_QUERY(metadata, '$.tags' RETURNING JSON)", sql)
        self.assertNotIn("JSON_EXISTS(metadata", sql)
        self.assertIn(" AND ", sql)
        self.assertNotIn("OR 1=1", sql)  # value only ever appears as a bind
        self.assertEqual(binds, {"tg0": "handoff", "tg1": "x' or 1=1 --"})
        sql_any, _ = oracle_plugin._tag_filter_sql(["a", "b"], "any")
        self.assertIn(" OR ", sql_any)
        self.assertEqual(oracle_plugin._tag_filter_sql(None), ("", {}))


class TestAddWithMemoryIdAndTags(unittest.TestCase):
    def test_add_with_tags_stores_metadata_tags(self):
        p, _, cur = _provider()
        out = json.loads(p.handle_tool_call("oracle_add", {"content": "fact one", "tags": ["Handoff", "project:x"]}))
        self.assertEqual(out["tags"], ["handoff", "project:x"])
        self.assertTrue(out["memory_id"].startswith("fact_"))
        sql, binds = cur.execute.call_args[0]
        self.assertIn("INSERT", sql)
        self.assertEqual(json.loads(binds[7]), {"kind": "tool", "tags": ["handoff", "project:x"]})

    def test_add_with_memory_id_inserts_when_absent(self):
        p, _, cur = _provider(rowcount=0)  # UPDATE finds nothing -> INSERT
        out = json.loads(
            p.handle_tool_call(
                "oracle_add", {"content": "state: X", "memory_id": "handoff:proj:topic", "tags": ["handoff"]}
            )
        )
        self.assertEqual(out["memory_id"], "handoff:proj:topic")
        self.assertFalse(out["upserted"])
        stmts = [c[0][0] for c in cur.execute.call_args_list]
        self.assertIn("UPDATE", stmts[0])
        self.assertIn("INSERT", stmts[1])
        self.assertEqual(cur.execute.call_args_list[1][0][1][0], "handoff:proj:topic")

    def test_add_with_existing_memory_id_updates_in_place(self):
        p, _, cur = _provider(rowcount=1)
        out = json.loads(
            p.handle_tool_call(
                "oracle_add", {"content": "state: Y", "memory_id": "handoff:proj:topic", "tags": ["handoff", "p"]}
            )
        )
        self.assertTrue(out["upserted"])
        self.assertEqual(cur.execute.call_count, 1)
        sql, binds = cur.execute.call_args[0]
        self.assertIn("UPDATE", sql)
        self.assertIn("JSON_TRANSFORM", sql)
        self.assertEqual(binds["id"], "handoff:proj:topic")
        self.assertEqual(json.loads(binds["tags"]), ["handoff", "p"])

    def test_add_upsert_without_tags_keeps_existing_tags(self):
        p, _, cur = _provider(rowcount=1)
        json.loads(p.handle_tool_call("oracle_add", {"content": "state: Z", "memory_id": "handoff:a"}))
        sql, binds = cur.execute.call_args[0]
        self.assertNotIn("JSON_TRANSFORM", sql)
        self.assertNotIn("tags", binds)

    def test_add_upsert_race_falls_back_to_update(self):
        p, _, cur = _provider(rowcount=0)
        calls = {"n": 0}

        def execute(sql, binds=None):
            calls["n"] += 1
            if "INSERT" in sql:
                raise Exception("ORA-00001: unique constraint (HERMES_AGENT_MEMORY.SYS_C001) violated")

        cur.execute.side_effect = execute
        out = json.loads(p.handle_tool_call("oracle_add", {"content": "s", "memory_id": "handoff:race"}))
        self.assertTrue(out["upserted"])
        self.assertEqual(calls["n"], 3)  # UPDATE(0 rows) -> INSERT(dup) -> UPDATE
        self.assertFalse(p._onnx_in_cooldown())

    def test_memory_id_with_multiple_facts_is_rejected(self):
        p, _, cur = _provider()
        out = p.handle_tool_call("oracle_add", {"facts": ["a", "b"], "memory_id": "handoff:x"})
        self.assertIn("single fact", out)
        cur.execute.assert_not_called()

    def test_invalid_memory_id_and_tags_are_tool_errors(self):
        p, _, cur = _provider()
        self.assertIn("memory_id must", p.handle_tool_call("oracle_add", {"content": "a", "memory_id": "bad id"}))
        self.assertIn("tag too long", p.handle_tool_call("oracle_add", {"content": "a", "tags": ["x" * 80]}))
        cur.execute.assert_not_called()

    def test_batch_add_carries_tags(self):
        p, _, _ = _provider()
        seen = []
        p._insert_many = MagicMock(side_effect=lambda rows: seen.extend(rows))
        p._insert = MagicMock(side_effect=lambda **kw: seen.append(kw))
        p.handle_tool_call("oracle_add", {"facts": ["a", "b"], "tags": ["batch"]})
        self.assertTrue(seen)
        for row in seen:
            self.assertEqual(row["metadata"].get("tags"), ["batch"])


class TestUpdateTags(unittest.TestCase):
    def test_tags_only_update(self):
        p, _, cur = _provider(rowcount=1)
        out = json.loads(p.handle_tool_call("oracle_update", {"memory_id": "handoff:x", "tags": ["done"]}))
        self.assertEqual(out["tags"], ["done"])
        sql, binds = cur.execute.call_args[0]
        self.assertIn("JSON_TRANSFORM", sql)
        self.assertNotIn("VECTOR_EMBEDDING", sql)  # no re-embed when content unchanged
        self.assertEqual(binds, {"id": "handoff:x", "tags": '["done"]'})

    def test_tags_only_update_works_during_onnx_cooldown(self):
        p, _, cur = _provider(rowcount=1)
        p._onnx_mark_failed("ORA-40284: model does not exist")
        self.assertTrue(p._onnx_in_cooldown())
        out = json.loads(p.handle_tool_call("oracle_update", {"memory_id": "handoff:x", "tags": []}))
        self.assertEqual(out["tags"], [])

    def test_update_requires_content_or_tags(self):
        p, _, _ = _provider()
        self.assertIn("required", p.handle_tool_call("oracle_update", {"memory_id": "m1"}))

    def test_update_missing_row(self):
        p, _, _ = _provider(rowcount=0)
        self.assertIn("Memory not found", p.handle_tool_call("oracle_update", {"memory_id": "nope", "tags": ["a"]}))


class TestOracleGet(unittest.TestCase):
    ROW = (
        "handoff:proj:topic", "sess-1", "agent-1", "fact", "memory", "state: X",
        {"kind": "tool", "tags": ["handoff", "project:proj"]},
        datetime(2026, 11, 14, 20, 0, tzinfo=timezone.utc), None,
    )

    def test_get_by_id_reports_missing(self):
        p, _, cur = _provider(fetch=[self.ROW])
        out = json.loads(p.handle_tool_call("oracle_get", {"memory_ids": ["handoff:proj:topic", "handoff:gone"]}))
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["missing"], ["handoff:gone"])
        r = out["results"][0]
        self.assertEqual(r["tags"], ["handoff", "project:proj"])
        self.assertEqual(r["created_at"], "2026-11-14T20:00:00+00:00")
        self.assertEqual(r["updated_at"], r["created_at"])
        sql, binds = cur.execute.call_args[0]
        self.assertIn("memory_id IN (:m0, :m1)", sql)
        self.assertNotIn("VECTOR", sql)
        self.assertEqual(binds["lim"], 2)

    def test_get_by_tags_any(self):
        p, _, cur = _provider(fetch=[self.ROW])
        json.loads(p.handle_tool_call("oracle_get", {"tags": ["Handoff", "x"], "tag_match": "any", "limit": 500}))
        sql, binds = cur.execute.call_args[0]
        self.assertIn(" OR ", sql)
        self.assertEqual(binds["tg0"], "handoff")
        self.assertEqual(binds["lim"], 100)  # clamped

    def test_get_metadata_as_json_string_or_lob(self):
        lob = MagicMock()
        lob.read.return_value = '{"tags":["a"]}'
        row = self.ROW[:5] + (lob, '{"tags":["a"]}') + self.ROW[7:]
        p, _, _ = _provider(fetch=[row])
        out = json.loads(p.handle_tool_call("oracle_get", {"memory_id": "handoff:proj:topic"}))
        self.assertEqual(out["results"][0]["content"], '{"tags":["a"]}')
        self.assertEqual(out["results"][0]["tags"], ["a"])

    def test_get_needs_a_selector(self):
        p, _, cur = _provider()
        self.assertIn("needs memory_id", p.handle_tool_call("oracle_get", {}))
        self.assertIn("needs memory_id", p.handle_tool_call("oracle_get", {"agent_id": "a"}))
        cur.execute.assert_not_called()

    def test_get_works_during_onnx_cooldown(self):
        p, _, _ = _provider(fetch=[self.ROW])
        p._onnx_mark_failed("ORA-40284: model does not exist")
        out = json.loads(p.handle_tool_call("oracle_get", {"tags": ["handoff"]}))
        self.assertEqual(out["count"], 1)

    def test_get_empty(self):
        p, _, _ = _provider(fetch=[])
        out = json.loads(p.handle_tool_call("oracle_get", {"memory_id": "handoff:none"}))
        self.assertEqual(out["count"], 0)
        self.assertEqual(out["missing"], ["handoff:none"])


class TestSearchTags(unittest.TestCase):
    def test_builders_add_bound_tag_predicate(self):
        p, _, _ = _provider()
        for sql, binds in (
            p._build_text_search_sql("q", 10, "", "any", tags=["handoff"]),
            p._build_vector_search_sql("q", None, 10, "", "any", tags=["handoff"]),
            p._build_hybrid_search_sql("q", None, "q", 10, "", "any", tags=["handoff"]),
        ):
            self.assertIn("JSON_EXISTS(JSON_QUERY(metadata", sql)
            self.assertEqual(binds["tg0"], "handoff")
        sql, binds = p._build_text_search_sql("q", 10, "", "any")
        self.assertNotIn("JSON_EXISTS", sql)
        self.assertNotIn("tg0", binds)

    def test_search_results_with_datetime_serialize(self):
        p, _, _ = _provider()
        p._search = MagicMock(
            return_value=[{"memory_id": "a", "content": "c", "score": 0.5,
                           "created_at": datetime(2026, 11, 1, tzinfo=timezone.utc)}]
        )
        out = json.loads(p.handle_tool_call("oracle_search", {"query": "c", "tags": "handoff"}))
        self.assertEqual(out["results"][0]["created_at"], "2026-11-01T00:00:00+00:00")
        self.assertEqual(p._search.call_args.kwargs["tags"], ["handoff"])

    def test_schemas_expose_handoff_fields(self):
        by_name = {s["name"]: s for s in oracle_plugin.TOOL_SCHEMAS}
        self.assertIn("memory_id", by_name["oracle_add"]["parameters"]["properties"])
        self.assertIn("tags", by_name["oracle_add"]["parameters"]["properties"])
        self.assertIn("tags", by_name["oracle_search"]["parameters"]["properties"])
        self.assertEqual(by_name["oracle_update"]["parameters"]["required"], ["memory_id"])
        self.assertIn("oracle_get", by_name)
        self.assertIn("10. HANDOFFS", oracle_plugin._PROMPT_BODY)


if __name__ == "__main__":
    unittest.main()
