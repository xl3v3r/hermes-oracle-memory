"""#30 adding memories: oracle_add reports near-duplicates of a single fact
and does not write a row that would be a copy of a stored one; the batch
response echoes tags; `facts` and `content` are parsed by one helper.
Fully mocked; no database."""

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


def _db_row(mid, content, dist, target="memory", role="fact", tags=None, session="sess-1", agent="hermes"):
    """A row as the vector leg returns it (see _fetch_rows)."""
    return (mid, session, agent, role, content, target, 0, dist, None, json.dumps(tags) if tags else None)


def _row(mid="fact_0123456789abcdef", content="The export runs nightly", similarity=1.0, **over):
    """A row as _similar_rows returns it."""
    row = {
        "memory_id": mid, "session_id": "sess-1", "agent_id": "hermes", "role": "fact",
        "content": content, "target": "memory", "similarity": similarity,
    }
    row.update(over)
    return row


def _add(p, **args):
    return json.loads(p.handle_tool_call("oracle_add", args))


class TestSimilarRows(unittest.TestCase):
    def test_returns_rows_at_or_above_the_floor(self):
        p, _, cur = _provider(add_similar_min=0.9)
        cur.fetchall.return_value = [
            _db_row("fact_a", "The export runs nightly at 02:00", 0.02, tags=["ops"]),
            _db_row("fact_b", "The export runs every night", 0.10),
            _db_row("fact_c", "Unrelated", 0.45),
        ]
        out = p._similar_rows("The export runs nightly at 02:00 ")
        self.assertEqual([(r["memory_id"], r["similarity"]) for r in out], [("fact_a", 0.98), ("fact_b", 0.9)])
        self.assertEqual(out[0]["tags"], ["ops"])
        self.assertEqual((out[0]["session_id"], out[0]["agent_id"], out[0]["role"]), ("sess-1", "hermes", "fact"))

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
        self.assertEqual(p._similar_reply(out, "same text")[0]["memory_id"], "handoff:x")  # without the scope prefix

    def test_long_text_is_embedded_like_a_stored_row(self):
        p, _, cur = _provider()
        long = "x" * 5000
        cur.fetchall.return_value = [_db_row("fact_a", long, 0.0)]
        p._similar_rows(long)
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


class TestSimilarReply(unittest.TestCase):
    def test_public_shape(self):
        p, _, _ = _provider()
        rows = [
            _row("fact_a", "The export runs nightly at 02:00", 0.98, tags=["ops"], session_id="other"),
            _row("fact_b", "The export runs every night", 0.9, target="user"),
        ]
        self.assertEqual(p._similar_reply(rows, "The export runs nightly at 02:00"), [
            {"memory_id": "fact_a", "score": 0.98, "content": "The export runs nightly at 02:00",
             "target": "memory", "tags": ["ops"], "identical": True},
            {"memory_id": "fact_b", "score": 0.9, "content": "The export runs every night", "target": "user"},
        ])

    def test_long_text_is_cut(self):
        p, _, _ = _provider()
        long = "x" * 5000
        out = p._similar_reply([_row(content=long)], long)
        self.assertEqual(out[0]["content"], "x" * 200 + "...")
        self.assertTrue(out[0]["identical"])  # compared in full, shown cut


class TestStoredCopy(unittest.TestCase):
    """A fact is left unwritten only when the row it would become already
    exists: same text, target, tags, session and agent, in a row oracle_add
    itself created."""

    TEXT = "The export runs nightly"

    def _copy(self, row, *, target="memory", tags=(), p=None):
        p = p or _provider()[0]
        return p._stored_copy([row], self.TEXT, target, list(tags))

    def test_the_same_row_is_a_copy(self):
        row = _row()
        self.assertIs(self._copy(row), row)
        tagged = _row(tags=["ops", "project:x"])
        self.assertIs(self._copy(tagged, tags=["project:x", "ops"]), tagged)

    def test_surrounding_white_space_in_the_stored_text_does_not_matter(self):
        self.assertIsNotNone(self._copy(_row(content="  The export runs nightly\n")))

    def test_what_makes_a_row_not_a_copy(self):
        cases = {
            "other text": _row(content="The export runs nightly."),
            "other case": _row(content="the export runs nightly"),
            "other target": _row(target="user"),
            "row has a tag the call does not": _row(tags=["ops"]),
            "other session": _row(session_id="sess-0"),
            "other agent": _row(agent_id="coder"),
            "caller-chosen id": _row("handoff:billing:export"),
            "write-ahead staging row": _row("wal_20261017_export"),
            "mirror of a built-in memory entry": _row("md_0123456789abcdef"),
            "imported chunk": _row("chk_0123456789abcdef"),
            "id that only looks generated": _row("fact_export"),
            "not a fact row": _row(role="user"),
        }
        for label, row in cases.items():
            self.assertIsNone(self._copy(row), label)
        self.assertIsNone(self._copy(_row(tags=["ops"]), tags=["ops", "project:x"]), "call has a tag the row does not")
        self.assertIsNone(self._copy(_row(), tags=["ops"]), "call has tags, row has none")

    def test_a_session_without_an_id_matches_the_placeholder_it_writes(self):
        p = _provider()[0]
        p._session_id = ""
        self.assertIsNotNone(self._copy(_row(session_id="none"), p=p))
        self.assertIsNone(self._copy(_row(session_id="sess-1"), p=p))

    def test_first_copy_among_the_rows(self):
        p = _provider()[0]
        other, copy = _row("fact_aaaaaaaaaaaaaaaa", session_id="sess-0"), _row("fact_bbbbbbbbbbbbbbbb")
        self.assertIs(p._stored_copy([other, copy], self.TEXT, "memory", []), copy)
        self.assertIsNone(p._stored_copy([], self.TEXT, "memory", []))


class TestAddReportsSimilar(unittest.TestCase):
    def test_single_fact_reports_similar_rows(self):
        p, _, _ = _provider()
        p._similar_rows = MagicMock(return_value=[_row("fact_b", "The export runs every night", 0.93)])
        p._insert = MagicMock()
        out = _add(p, content="The export runs nightly")
        self.assertEqual(out["result"], "Fact stored.")
        self.assertEqual(out["similar"], [
            {"memory_id": "fact_b", "score": 0.93, "content": "The export runs every night", "target": "memory"},
        ])
        p._insert.assert_called_once()
        p._similar_rows.assert_called_once_with("The export runs nightly")

    def test_no_similar_key_when_nothing_is_close(self):
        p, _, _ = _provider()
        p._similar_rows = MagicMock(return_value=[])
        p._insert = MagicMock()
        self.assertNotIn("similar", _add(p, content="A new fact"))

    def test_a_copy_of_a_stored_row_is_not_written(self):
        p, _, _ = _provider()
        p._similar_rows = MagicMock(return_value=[_row("fact_aaaaaaaaaaaaaaaa")])
        p._insert = MagicMock()
        out = _add(p, content="The export runs nightly")
        self.assertEqual(out, {
            "result": "This fact is already stored, by this session, with the same target and tags; nothing was added.",
            "memory_id": "fact_aaaaaaaaaaaaaaaa",
            "duplicate": True,
        })
        p._insert.assert_not_called()

    def test_identical_text_from_another_session_is_stored_and_reported(self):
        p, _, _ = _provider()
        p._similar_rows = MagicMock(return_value=[_row("fact_aaaaaaaaaaaaaaaa", session_id="sess-0")])
        p._insert = MagicMock()
        out = _add(p, content="The export runs nightly")
        p._insert.assert_called_once()
        self.assertNotIn("duplicate", out)
        self.assertEqual(out["similar"][0]["memory_id"], "fact_aaaaaaaaaaaaaaaa")
        self.assertTrue(out["similar"][0]["identical"])

    def test_identical_text_under_another_target_is_stored(self):
        p, _, _ = _provider()
        p._similar_rows = MagicMock(return_value=[_row(content="Prefers short answers", target="user")])
        p._insert = MagicMock()
        out = _add(p, content="Prefers short answers", target="memory")
        p._insert.assert_called_once()
        self.assertNotIn("duplicate", out)

    def test_identical_text_with_other_tags_is_stored(self):
        for stored, asked in ((["ops"], ["ops", "project:x"]), (["ops", "project:x"], ["ops"]), (["ops"], None)):
            p, _, _ = _provider()
            p._similar_rows = MagicMock(return_value=[_row(content="Deploy on Fridays", tags=stored)])
            p._insert = MagicMock()
            args = {"content": "Deploy on Fridays"}
            if asked is not None:
                args["tags"] = asked
            out = _add(p, **args)
            p._insert.assert_called_once()
            self.assertNotIn("duplicate", out, (stored, asked))

    def test_tags_are_compared_as_normalised(self):
        p, _, _ = _provider()
        p._similar_rows = MagicMock(return_value=[_row(content="Deploy on Fridays", tags=["ops", "project:x"])])
        p._insert = MagicMock()
        out = _add(p, content="Deploy on Fridays", tags=["Project:X", " OPS "])
        p._insert.assert_not_called()
        self.assertTrue(out["duplicate"])

    def test_a_staging_row_never_stands_in_for_the_fact(self):
        # Write-ahead: the staging row is deleted once the final row is in.
        p, _, _ = _provider()
        p._similar_rows = MagicMock(return_value=[_row("wal_20261017_export")])
        p._insert = MagicMock()
        out = _add(p, content="The export runs nightly")
        p._insert.assert_called_once()
        self.assertNotIn("duplicate", out)

    def test_lookup_runs_before_the_insert(self):
        p, _, _ = _provider()
        order = []
        p._similar_rows = MagicMock(side_effect=lambda *a, **k: order.append("lookup") or [])
        p._insert = MagicMock(side_effect=lambda **k: order.append("insert"))
        _add(p, content="A new fact")
        self.assertEqual(order, ["lookup", "insert"])

    def test_upsert_reports_similar_but_always_writes(self):
        p, _, _ = _provider()
        p._similar_rows = MagicMock(return_value=[_row("fact_aaaaaaaaaaaaaaaa", "State one")])
        p._upsert_fact = MagicMock(return_value={"result": "Fact stored.", "memory_id": "handoff:x", "upserted": False})
        out = _add(p, content="State one", memory_id="handoff:x")
        p._upsert_fact.assert_called_once()
        self.assertEqual(out["memory_id"], "handoff:x")
        self.assertEqual(out["similar"][0]["memory_id"], "fact_aaaaaaaaaaaaaaaa")
        self.assertTrue(out["similar"][0]["identical"])
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
