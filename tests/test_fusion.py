"""#29 retrieval: the two search legs are merged the same way whatever order
their rows arrive in; HERMES_ORACLE_RANKING=rrf replaces the weighted sum
with reciprocal-rank fusion and picks the text leg's terms by length; a
query with no usable term has no text leg; ids such as "testing-1" are no
longer hidden. Fully mocked; no database."""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401  (stubs agent.* / tools.*)
import __init__ as oracle_plugin  # noqa: E402

NAME = "HERMES_ORACLE_RANKING"
T0 = datetime(2026, 11, 1)


def vec(mid, similarity, age_days=0):
    """A vector-leg row as _fetch_rows maps it."""
    return {"memory_id": mid, "role": "fact", "content": mid, "score": round(0.7 * similarity, 4),
            "dist": round(1.0 - similarity, 4), "created_at": T0 - timedelta(days=age_days)}


def txt(mid, text_score, age_days=0):
    """A text-leg row as _fetch_rows maps it (SCORE()/100, no distance)."""
    return {"memory_id": mid, "role": "fact", "content": mid, "score": round(text_score, 4),
            "dist": None, "created_at": T0 - timedelta(days=age_days)}


def scores(rows, ranking):
    return {r["memory_id"]: r["score"] for r in oracle_plugin._fuse_legs(rows, ranking)}


def order(rows, ranking):
    fused = oracle_plugin._fuse_legs(rows, ranking)
    return [r["memory_id"] for r in sorted(fused, key=lambda r: r["score"], reverse=True)]


def _provider(**tuning):
    p = oracle_plugin.OracleMemoryProvider()
    p._table = "HERMES_AGENT_MEMORY"
    p._onnx_model = "BGE_BASE_EN_V1_5"
    p._tuning = dict(oracle_plugin._read_tuning(use_module_values=True), **tuning)
    conn, cur = MagicMock(), MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    p._pool = MagicMock()
    p._pool.acquire.return_value = conn
    p._onnx_in_cooldown = lambda: False
    return p, cur


class TestWeighted(unittest.TestCase):
    ROWS = [vec("both", 0.8), vec("vec-only", 0.6), txt("both", 0.5), txt("text-only", 0.9)]

    def test_scores_are_the_weighted_sum(self):
        self.assertEqual(scores(self.ROWS, "weighted"), {"both": 0.71, "vec-only": 0.42, "text-only": 0.27})

    def test_row_order_does_not_matter(self):
        want = scores(self.ROWS, "weighted")
        self.assertEqual(scores(list(reversed(self.ROWS)), "weighted"), want)
        self.assertEqual(scores([self.ROWS[2], self.ROWS[0], self.ROWS[3], self.ROWS[1]], "weighted"), want)

    def test_sum_is_capped_at_one(self):
        self.assertEqual(scores([vec("a", 1.0), txt("a", 1.0)], "weighted"), {"a": 1.0})

    def test_fused_rows_carry_no_distance(self):
        for r in oracle_plugin._fuse_legs(self.ROWS, "weighted"):
            self.assertNotIn("dist", r)

    def test_input_rows_are_not_changed(self):
        rows = [vec("a", 0.8), txt("a", 0.5)]
        oracle_plugin._fuse_legs(rows, "weighted")
        self.assertEqual(rows, [vec("a", 0.8), txt("a", 0.5)])

    def test_a_keyword_only_hit_cannot_pass_a_modest_semantic_hit(self):
        # The limit the issue describes: 0.3 at most against 0.7 x similarity.
        self.assertEqual(order([vec("semantic", 0.45), txt("identifier", 1.0)], "weighted"), ["semantic", "identifier"])


class TestRrf(unittest.TestCase):
    def test_first_in_both_legs_scores_one(self):
        self.assertEqual(scores([vec("a", 0.9), txt("a", 0.4)], "rrf"), {"a": 1.0})

    def test_first_in_one_leg_scores_a_half(self):
        self.assertEqual(scores([vec("v", 0.5), txt("t", 0.2)], "rrf"), {"v": 0.5, "t": 0.5})

    def test_score_follows_rank_not_raw_score(self):
        rows = [vec("v1", 0.99), vec("v2", 0.98), vec("v3", 0.10)]
        got = scores(rows, "rrf")
        self.assertEqual(got, {"v1": 0.5, "v2": round(61 / 62 / 2, 4), "v3": round(61 / 63 / 2, 4)})

    def test_the_best_keyword_hit_ranks_with_the_best_semantic_hit(self):
        rows = [vec("s1", 0.9), vec("s2", 0.85), vec("s3", 0.8), txt("identifier", 0.3)]
        got = order(rows, "rrf")
        self.assertLess(got.index("identifier"), got.index("s2"))

    def test_a_row_in_both_legs_beats_a_row_in_one(self):
        rows = [vec("v-only", 0.95), vec("both", 0.6), txt("t-only", 0.9), txt("both", 0.5)]
        self.assertEqual(order(rows, "rrf")[0], "both")

    def test_row_order_does_not_matter(self):
        rows = [vec("a", 0.9), vec("b", 0.8), vec("c", 0.7), txt("c", 0.9), txt("d", 0.5), txt("a", 0.2)]
        want = scores(rows, "rrf")
        self.assertEqual(scores(list(reversed(rows)), "rrf"), want)
        self.assertEqual(scores(rows[3:] + rows[:3], "rrf"), want)

    def test_equal_keys_rank_newest_first_as_the_statement_orders_them(self):
        rows = [vec("old", 0.8, age_days=30), vec("new", 0.8, age_days=1), txt("t-old", 0.5, 9), txt("t-new", 0.5, 2)]
        got = scores(rows, "rrf")
        self.assertGreater(got["new"], got["old"])
        self.assertGreater(got["t-new"], got["t-old"])

    def test_rows_without_a_time(self):
        rows = [dict(vec("a", 0.8), created_at=None), dict(vec("b", 0.7), created_at=None)]
        self.assertEqual(order(rows, "rrf"), ["a", "b"])


class TestHybridSearch(unittest.TestCase):
    def _run(self, rows, ranking="weighted", contains_q="{alpha}"):
        p, cur = _provider(ranking=ranking)
        p._fetch_rows_attempt = MagicMock(return_value=rows)
        out = p._execute_hybrid_search(cur, "alpha", contains_q, 50)
        return p, out

    def test_text_rows_first_give_the_same_result(self):
        rows = [vec("both", 0.8), vec("vec-only", 0.6), txt("both", 0.5), txt("text-only", 0.9)]
        _, a = self._run(rows)
        _, b = self._run(list(reversed(rows)))
        self.assertEqual([(r["memory_id"], r["score"]) for r in a], [("both", 0.71), ("vec-only", 0.42), ("text-only", 0.27)])
        self.assertEqual([(r["memory_id"], r["score"]) for r in b], [(r["memory_id"], r["score"]) for r in a])

    def test_setting_selects_the_fusion(self):
        rows = [vec("semantic", 0.9), vec("other", 0.5), txt("identifier", 0.3)]
        _, weighted = self._run(rows, "weighted")
        _, rrf = self._run(rows, "rrf")
        self.assertEqual([r["memory_id"] for r in weighted], ["semantic", "other", "identifier"])
        self.assertEqual({r["memory_id"]: r["score"] for r in rrf}["identifier"], 0.5)

    def test_prefix_boost_still_applies(self):
        rows = [dict(vec("a", 0.5), content="alpha is first"), dict(vec("b", 0.6), content="something else")]
        _, out = self._run(rows)
        self.assertEqual([(r["memory_id"], r["score"]) for r in out], [("a", 0.5), ("b", 0.42)])

    def test_no_usable_term_runs_the_vector_leg_alone(self):
        p, out = self._run([vec("a", 0.8)], contains_q="")
        sql, binds = p._fetch_rows_attempt.call_args[0][1], p._fetch_rows_attempt.call_args[0][2]
        self.assertNotIn("CONTAINS", sql)
        self.assertNotIn("q", binds)
        self.assertEqual([(r["memory_id"], r["score"]) for r in out], [("a", 0.56)])

    def test_no_usable_term_during_a_cooldown_finds_nothing(self):
        p, cur = _provider()
        p._onnx_in_cooldown = lambda: True
        p._fetch_rows = MagicMock()
        self.assertEqual(p._execute_hybrid_search(cur, "??", "", 50), [])
        p._fetch_rows.assert_not_called()

    def test_text_only_paths_use_the_same_fusion(self):
        for ranking, want in (("weighted", 0.27), ("rrf", 0.5)):
            p, cur = _provider(ranking=ranking)
            p._onnx_in_cooldown = lambda: True
            p._fetch_rows = MagicMock(return_value=[txt("t", 0.9)])
            out = p._execute_hybrid_search(cur, "alpha", "{alpha}", 50)
            self.assertEqual([(r["memory_id"], r["score"]) for r in out], [("t", want)], ranking)


class TestTextLegTerms(unittest.TestCase):
    LONG = "please tell me what the status of the printer ticket 90417 is after the firmware rollback on gateway-07"

    def test_no_usable_term_means_no_text_leg(self):
        p, _ = _provider()
        for query in (None, "", "   \n\t ", "?!.,;:()", "a to is", "or"):
            self.assertEqual(p._contains_query(query), "", query)

    def test_weighted_keeps_the_first_eight_words(self):
        p, _ = _provider()
        self.assertEqual(
            p._contains_query(self.LONG),
            "{please} ACCUM {tell} ACCUM {what} ACCUM {the} ACCUM {status} ACCUM {the} ACCUM {printer} ACCUM {ticket}",
        )

    def test_rrf_drops_stop_words_and_keeps_the_eight_longest_terms_in_order(self):
        p, _ = _provider(ranking="rrf")
        self.assertEqual(
            p._contains_query(self.LONG),
            "{please} ACCUM {status} ACCUM {printer} ACCUM {ticket} ACCUM {90417} ACCUM {firmware} ACCUM {rollback} ACCUM {gateway}",
        )

    def test_rrf_short_query_is_unchanged_apart_from_stop_words(self):
        p, _ = _provider(ranking="rrf")
        self.assertEqual(p._contains_query("billing export schedule"), "{billing} ACCUM {export} ACCUM {schedule}")
        self.assertEqual(p._contains_query("the plan for tomorrow"), "{plan} ACCUM {tomorrow}")
        self.assertEqual(p._contains_query("Export export EXPORT nightly"), "{Export} ACCUM {nightly}")

    def test_rrf_only_stop_words_means_no_text_leg(self):
        p, _ = _provider(ranking="rrf")
        self.assertEqual(p._contains_query("what was that about"), "")

    def test_text_mode_without_a_term_returns_nothing_and_asks_nothing(self):
        p, cur = _provider()
        p._fetch_rows = MagicMock()
        self.assertEqual(p._search_once("?!", mode="text"), [])
        p._fetch_rows.assert_not_called()


class TestJunkFilter(unittest.TestCase):
    def test_underscore_is_literal(self):
        junk = oracle_plugin.OracleMemoryProvider._JUNK
        self.assertIn("memory_id NOT LIKE 'test\\_%' ESCAPE '\\'", junk)
        self.assertNotIn("'test_%'", junk)


class TestSetting(unittest.TestCase):
    def test_values(self):
        parse = oracle_plugin._parse_ranking
        for raw, want in ((None, "weighted"), ("", "weighted"), ("weighted", "weighted"), ("RRF", "rrf"), (" rrf ", "rrf")):
            self.assertEqual(parse(raw), want, raw)

    def test_an_unknown_value_keeps_the_default(self):
        with self.assertLogs(oracle_plugin.logger, level="WARNING") as logs:
            self.assertEqual(oracle_plugin._parse_ranking("rff"), "weighted")
        self.assertIn(NAME, logs.output[0])

    def test_read_per_profile(self):
        for value, want in ((None, "weighted"), ("rrf", "rrf")):
            env = {} if value is None else {NAME: value}
            with patch.object(oracle_plugin, "get_secret", return_value=None), patch.dict(os.environ, env):
                if value is None:
                    os.environ.pop(NAME, None)
                self.assertEqual(oracle_plugin._read_tuning()["ranking"], want, value)


if __name__ == "__main__":
    unittest.main()
