"""#28 item 2: queue_prefetch starts no search.

Hermes calls queue_prefetch after a completed turn with the message that was
just answered; prefetch() at the start of the next turn asks for the next
message and runs its own search. Fully mocked; no database."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401  (stubs agent.* / tools.*)
import __init__ as oracle_plugin  # noqa: E402

ANSWERED = "what is the schedule of the billing export"
NEXT = "and who owns the invoice backfill job"
ROW = {"memory_id": "fact_1", "role": "fact", "target": "memory", "score": 0.9, "content": "The export runs nightly"}


def _provider():
    p = oracle_plugin.OracleMemoryProvider()
    p._pool = MagicMock()
    p._search = MagicMock(return_value=[ROW])
    return p


class TestQueuePrefetch(unittest.TestCase):
    def test_starts_no_thread_and_no_search(self):
        p = _provider()
        with patch.object(oracle_plugin, "_spawn_thread") as spawn:
            self.assertIsNone(p.queue_prefetch(ANSWERED, session_id="s1"))
        spawn.assert_not_called()
        p._search.assert_not_called()
        p._pool.acquire.assert_not_called()

    def test_leaves_this_turns_recall_status_alone(self):
        p = _provider()
        out = p.prefetch(ANSWERED)
        self.assertIn("The export runs nightly", out)
        self.assertEqual(p.recall_status().count, 1)
        p.queue_prefetch(ANSWERED, session_id="s1")
        self.assertEqual(p.recall_status().count, 1)
        self.assertEqual(p._search.call_count, 1)

    def test_next_turn_searches_for_its_own_message(self):
        p = _provider()
        p.queue_prefetch(ANSWERED)
        p.on_turn_start(2, NEXT)
        self.assertIn("The export runs nightly", p.prefetch(NEXT))
        self.assertEqual([c.args[0] for c in p._search.call_args_list], [NEXT])

    def test_a_repeated_message_is_searched_again(self):
        p = _provider()
        self.assertTrue(p.prefetch(ANSWERED))
        p.queue_prefetch(ANSWERED)
        self.assertTrue(p.prefetch(ANSWERED))
        self.assertEqual(p._search.call_count, 2)

    def test_accepts_what_hermes_passes(self):
        p = _provider()
        for query in ("", None, 42, "ok"):
            self.assertIsNone(p.queue_prefetch(query, session_id=""))
        p._search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
