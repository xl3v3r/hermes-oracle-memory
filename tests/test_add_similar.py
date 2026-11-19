"""#30 adding memories: oracle_add reports near-duplicates of a single fact
and does not store an identical one twice; the batch response echoes tags;
`facts` and `content` are parsed by one helper. Fully mocked; no database."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401  (stubs agent.* / tools.*)
import __init__ as oracle_plugin  # noqa: E402


def _provider(**tuning):
    p = oracle_plugin.OracleMemoryProvider()
    p._table = "HERMES_AGENT_MEMORY"
    p._onnx_model = "BGE_BASE_EN_V1_5"
    p._session_id = "sess-1"
    p._tuning = dict(oracle_plugin._read_tuning(use_module_values=True), **tuning)
    conn, cur = MagicMock(), MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    p._pool = MagicMock()
    p._pool.acquire.return_value = conn
    return p, conn, cur


def _db_row(mid, content, dist, target="memory", role="fact", tags=None):
    """A row as the vector leg returns it (see _fetch_rows)."""
    return (mid, "s", "a", role, content, target, 0, dist, None, json.dumps(tags) if tags else None)


def _add(p, **args):
    return json.loads(p.handle_tool_call("oracle_add", args))


class TestSimilarRows(unittest.TestCase):
    def test_returns_rows_at_or_above_the_floor(self):
        p, _, cur = _provider(add_similar_min=0.9)
        cur.fetchall.return_value = [
            _db_row("fact_a", "The export runs nightly at 02:00", 0.02),
            _db_row("fact_b", "The export runs every night", 0.10),
            _db_row("fact_c", "Unrelated", 0.45),
        ]
        out = p._similar_rows("The export runs nightly at 02:00 ")
        self.assertEqual(out, [
            {"memory_id": "fact_a", "score": 0.98, "content": "The export runs nightly at 02:00",
             "target": "memory", "identical": True},
            {"memory_id": "fact_b", "score": 0.9, "content": "The export runs every night", "target": "memory"},
        ])

    def test_statement_is_the_vector_leg_without_skills(self):
        p, _, cur = _provider()
        cur.fetchall.return_value = []
        p._similar_rows("some new fact", exclude_ids=("handoff:x", "s.abc:handoff:x"))
        sql, binds = cur.execute.call_args[0][0], cur.execute.call_args[0][1]
        self.assertIn("VECTOR_DISTANCE(embedding, VECTOR_EMBEDDING(BGE_BASE_EN_V1_5 USING :qv AS DATA), COSINE)", sql)
        self.assertIn("NVL(role, 'fact') <> 'skill'", sql)
        self.assertIn("memory_id NOT IN (:x0, :x1)", sql)
        self.assertIn("memory_id NOT LIKE", sql)  # the junk filter every search uses
        self.assertEqual((binds["x0"], binds["x1"]), ("handoff:x", "s.abc:handoff:x"))
        self.assertEqual(binds["qv"], "some new fact")
        self.assertEqual(binds["k"], oracle_plugin._ADD_SIMILAR_MAX)
        self.assertNotIn("scope", binds)

    def test_scoped_session_only_sees_its_own_rows(self):
        p, _, cur = _provider()
        p._scope_id = "owui:alice"
        cur.fetchall.return_value = [_db_row(p._store_id("handoff:x"), "same text", 0.0)]
        out = p._similar_rows("same text")
        sql, binds = cur.execute.call_args[0][0], cur.execute.call_args[0][1]
        # _SCOPE_READ also admits the shared skill corpus; skills are excluded here.
        self.assertIn("(scope_id = :scope OR role = 'skill')", sql)
        self.assertIn("NVL(role, 'fact') <> 'skill'", sql)
        self.assertEqual(binds["scope"], "owui:alice")
        self.assertNotEqual(p._store_id("handoff:x"), "handoff:x")
        self.assertEqual(out[0]["memory_id"], "handoff:x")  # shown without the scope prefix

    def test_long_text_is_cut_in_the_answer_and_embedded_like_a_stored_row(self):
        p, _, cur = _provider()
        long = "x" * 5000
        cur.fetchall.return_value = [_db_row("fact_a", long, 0.0)]
        out = p._similar_rows(long)
        self.assertEqual(out[0]["content"], "x" * 200 + "...")
        self.assertTrue(out[0]["identical"])
        self.assertEqual(cur.execute.call_args[0][1]["qv"], p._safe_embed_text(long))

    def test_call_has_its_own_short_timeout(self):
        p, conn, cur = _provider()
        p._search_timeout_ms = 13000
        cur.fetchall.return_value = []
        p._similar_rows("a fact")
        self.assertEqual(conn.call_timeout, oracle_plugin._ADD_SIMILAR_TIMEOUT_MS)
        p._search_timeout_ms = 1500
        p._similar_rows("a fact")
        self.assertEqual(conn.call_timeout, 1500)

    def test_off_when_the_floor_is_zero(self):
        p, _, _ = _provider(add_similar_min=0)
        self.assertEqual(p._similar_rows("a fact"), [])
        p._pool.acquire.assert_not_called()

    def test_off_during_an_onnx_cooldown(self):
        p, _, _ = _provider()
        p._onnx_in_cooldown = lambda: True
        self.assertEqual(p._similar_rows("a fact"), [])
        p._pool.acquire.assert_not_called()

    def test_a_failed_lookup_gives_no_rows_and_no_cooldown(self):
        p, _, cur = _provider()
        cur.execute.side_effect = RuntimeError("ORA-40284: model does not exist")
        p._onnx_mark_failed = MagicMock()
        self.assertEqual(p._similar_rows("a fact"), [])
        p._onnx_mark_failed.assert_not_called()

    def test_setting(self):
        name = "HERMES_ORACLE_ADD_SIMILAR_MIN"
        for value, want in ((None, 0.9), ("0", 0.0), ("0.8", 0.8), ("junk", 0.9), ("1.5", 1.0)):
            env = {} if value is None else {name: value}
            with patch.object(oracle_plugin, "get_secret", return_value=None), patch.dict(os.environ, env):
                if value is None:
                    os.environ.pop(name, None)
                self.assertEqual(oracle_plugin._read_tuning()["add_similar_min"], want, value)


class TestAddReportsSimilar(unittest.TestCase):
    def test_single_fact_reports_similar_rows(self):
        p, _, _ = _provider()
        similar = [{"memory_id": "fact_b", "score": 0.93, "content": "The export runs every night", "target": "memory"}]
        p._similar_rows = MagicMock(return_value=similar)
        p._insert = MagicMock()
        out = _add(p, content="The export runs nightly")
        self.assertEqual(out["result"], "Fact stored.")
        self.assertEqual(out["similar"], similar)
        p._insert.assert_called_once()
        p._similar_rows.assert_called_once_with("The export runs nightly")

    def test_no_similar_key_when_nothing_is_close(self):
        p, _, _ = _provider()
        p._similar_rows = MagicMock(return_value=[])
        p._insert = MagicMock()
        self.assertNotIn("similar", _add(p, content="A new fact"))

    def test_identical_fact_is_not_stored_twice(self):
        p, _, _ = _provider()
        p._similar_rows = MagicMock(return_value=[
            {"memory_id": "fact_a", "score": 1.0, "content": "The export runs nightly", "target": "memory",
             "identical": True},
        ])
        p._insert = MagicMock()
        out = _add(p, content="The export runs nightly")
        self.assertEqual(out, {
            "result": "An identical fact is already stored; nothing was added.",
            "memory_id": "fact_a",
            "duplicate": True,
        })
        p._insert.assert_not_called()

    def test_identical_text_under_another_target_is_stored(self):
        p, _, _ = _provider()
        p._similar_rows = MagicMock(return_value=[
            {"memory_id": "fact_a", "score": 1.0, "content": "Prefers short answers", "target": "user", "identical": True},
        ])
        p._insert = MagicMock()
        out = _add(p, content="Prefers short answers", target="memory")
        p._insert.assert_called_once()
        self.assertNotIn("duplicate", out)
        self.assertEqual(out["similar"][0]["memory_id"], "fact_a")

    def test_identical_text_with_new_tags_is_stored(self):
        p, _, _ = _provider()
        p._similar_rows = MagicMock(return_value=[
            {"memory_id": "fact_a", "score": 1.0, "content": "Deploy on Fridays", "target": "memory",
             "tags": ["ops"], "identical": True},
        ])
        p._insert = MagicMock()
        out = _add(p, content="Deploy on Fridays", tags=["ops", "project:x"])
        p._insert.assert_called_once()
        self.assertNotIn("duplicate", out)

    def test_identical_text_whose_tags_cover_the_request_is_not_stored(self):
        p, _, _ = _provider()
        p._similar_rows = MagicMock(return_value=[
            {"memory_id": "fact_a", "score": 1.0, "content": "Deploy on Fridays", "target": "memory",
             "tags": ["ops", "project:x"], "identical": True},
        ])
        p._insert = MagicMock()
        out = _add(p, content="Deploy on Fridays", tags=["OPS"])
        p._insert.assert_not_called()
        self.assertEqual(out["memory_id"], "fact_a")
        self.assertTrue(out["duplicate"])

    def test_lookup_runs_before_the_insert(self):
        p, _, _ = _provider()
        order = []
        p._similar_rows = MagicMock(side_effect=lambda *a, **k: order.append("lookup") or [])
        p._insert = MagicMock(side_effect=lambda **k: order.append("insert"))
        _add(p, content="A new fact")
        self.assertEqual(order, ["lookup", "insert"])

    def test_upsert_reports_similar_but_always_writes(self):
        p, _, _ = _provider()
        p._similar_rows = MagicMock(return_value=[
            {"memory_id": "fact_a", "score": 1.0, "content": "State one", "target": "memory", "identical": True},
        ])
        p._upsert_fact = MagicMock(return_value={"result": "Fact stored.", "memory_id": "handoff:x", "upserted": False})
        out = _add(p, content="State one", memory_id="handoff:x")
        p._upsert_fact.assert_called_once()
        self.assertEqual(out["memory_id"], "handoff:x")
        self.assertEqual(out["similar"][0]["memory_id"], "fact_a")
        p._similar_rows.assert_called_once_with("State one", exclude_ids=("handoff:x",))

    def test_upsert_in_a_scoped_session_excludes_the_stored_id(self):
        p, _, _ = _provider()
        p._scope_id = "owui:alice"
        p._scope_inherited = True  # so the call needs no caller key
        p._similar_rows = MagicMock(return_value=[])
        p._upsert_fact = MagicMock(return_value={"result": "Fact stored.", "memory_id": "handoff:x", "upserted": False})
        _add(p, content="State one", memory_id="handoff:x")
        # The row may be stored under the id as given or under the scope's prefix.
        p._similar_rows.assert_called_once_with(
            "State one", exclude_ids=("handoff:x", p._store_id("handoff:x"))
        )

    def test_batch_is_not_looked_up_and_echoes_tags(self):
        p, _, _ = _provider()
        p._similar_rows = MagicMock()
        p._insert_many = MagicMock()
        out = _add(p, facts=["one fact", "another fact"], tags=["Project:X"])
        p._similar_rows.assert_not_called()
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["tags"], ["project:x"])
        self.assertNotIn("tags", _add(p, facts=["one fact", "another fact"]))


class TestFactParsing(unittest.TestCase):
    def _stored(self, **args):
        p, _, _ = _provider(add_similar_min=0)
        p._insert = MagicMock()
        p._insert_many = MagicMock()
        p.handle_tool_call("oracle_add", args)
        if p._insert.called:
            return [p._insert.call_args.kwargs["content"]]
        if p._insert_many.called:
            return [r["content"] for r in p._insert_many.call_args[0][0]]
        return []

    def test_both_parameters_take_the_same_shapes(self):
        shapes = (
            ("one fact", ["one fact"]),
            ("  padded  ", ["padded"]),
            (["a", " b ", "", None], ["a", "b"]),
            ('["a", "b"]', ["a", "b"]),
            ([{"fact": "a"}, {"content": "b"}, {"text": "c"}, {"other": "d"}], ["a", "b", "c"]),
            ("[not json", ["[not json"]),
            ('{"a": 1}', ['{"a": 1}']),
        )
        for value, want in shapes:
            self.assertEqual(self._stored(facts=value), want, ("facts", value))
            self.assertEqual(self._stored(content=value), want, ("content", value))

    def test_facts_wins_over_content(self):
        self.assertEqual(self._stored(facts=["a"], content="b"), ["a"])

    def test_empty_facts_falls_back_to_content(self):
        self.assertEqual(self._stored(facts=[], content="b"), ["b"])
        self.assertEqual(self._stored(facts="  ", content="b"), ["b"])

    def test_scalar_content_is_stored_as_text(self):
        self.assertEqual(self._stored(content=42), ["42"])

    def test_scalar_facts_is_not_a_fact(self):
        self.assertEqual(self._stored(facts=42), [])


if __name__ == "__main__":
    unittest.main()
