"""#28 item 1: HERMES_ORACLE_CAPTURE names the rows the provider writes on
its own: a row per turn, a row per ended session, a row per skill use.
Turn and session rows are on by default; skill-use rows are off.
Fully mocked; no database."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401  (stubs agent.* / tools.*)
import __init__ as oracle_plugin  # noqa: E402

NAME = "HERMES_ORACLE_CAPTURE"
USER = "what is the schedule of the billing export"
MESSAGES = [{"role": "user", "content": "a message long enough to be kept as session notes"}]


def _provider(capture=None):
    p = oracle_plugin.OracleMemoryProvider()
    p._pool = MagicMock()
    p._session_id = "sess-1"
    p._agent_context = "primary"
    if capture is not None:
        p._tuning = dict(oracle_plugin._read_tuning(use_module_values=True), capture=frozenset(capture))
    p._queue_background_insert = MagicMock()
    return p


def _roles(p):
    return [(c.kwargs["role"], (c.kwargs.get("metadata") or {}).get("kind")) for c in p._queue_background_insert.call_args_list]


def _all_three(p):
    p.sync_turn(USER, "it runs nightly")
    p.on_session_end(MESSAGES)
    p._on_skill_lifecycle_impl(action="used", skill_name="pdf", session_id="sess-1")
    return _roles(p)


class TestParse(unittest.TestCase):
    def test_values(self):
        parse = oracle_plugin._parse_capture
        both = frozenset({"turn", "session"})
        cases = {
            "": both,
            None: both,
            "   ": both,
            "all": frozenset({"turn", "session", "skill_usage"}),
            "ALL": frozenset({"turn", "session", "skill_usage"}),
            "none": frozenset(),
            "off": frozenset(),
            "0": frozenset(),
            "turn": frozenset({"turn"}),
            "session, turn": both,
            "Turn SESSION": both,
            "turn,skill_usage": frozenset({"turn", "skill_usage"}),
            "skill-usage": frozenset({"skill_usage"}),
        }
        for raw, want in cases.items():
            self.assertEqual(parse(raw), want, raw)

    def test_an_unknown_name_captures_less_not_more(self):
        with self.assertLogs(oracle_plugin.logger, level="WARNING") as logs:
            self.assertEqual(oracle_plugin._parse_capture("turns"), frozenset())
            self.assertEqual(oracle_plugin._parse_capture("turn, sessions"), frozenset({"turn"}))
        self.assertIn(NAME, logs.output[0])
        self.assertIn("turns", logs.output[0])

    def test_setting_is_read_per_profile(self):
        for value, want in ((None, {"turn", "session"}), ("none", set()), ("all", {"turn", "session", "skill_usage"})):
            env = {} if value is None else {NAME: value}
            with patch.object(oracle_plugin, "get_secret", return_value=None), patch.dict(os.environ, env):
                if value is None:
                    os.environ.pop(NAME, None)
                self.assertEqual(oracle_plugin._read_tuning()["capture"], frozenset(want), value)
        with patch.object(oracle_plugin, "get_secret", side_effect={NAME: "session"}.get), patch.dict(os.environ, {NAME: "all"}):
            self.assertEqual(oracle_plugin._read_tuning()["capture"], frozenset({"session"}))


class TestWhatIsWritten(unittest.TestCase):
    def test_default_writes_turn_and_session_rows_only(self):
        self.assertEqual(_all_three(_provider({"turn", "session"})), [("turn", "turn"), ("session", "session_end")])
        self.assertEqual(oracle_plugin._parse_capture(""), frozenset({"turn", "session"}))

    def test_nothing(self):
        self.assertEqual(_all_three(_provider(())), [])

    def test_each_kind_alone(self):
        self.assertEqual(_all_three(_provider({"turn"})), [("turn", "turn")])
        self.assertEqual(_all_three(_provider({"session"})), [("session", "session_end")])
        self.assertEqual(_all_three(_provider({"skill_usage"})), [("fact", "skill_usage")])

    def test_all(self):
        self.assertEqual(
            _all_three(_provider({"turn", "session", "skill_usage"})),
            [("turn", "turn"), ("session", "session_end"), ("fact", "skill_usage")],
        )

    def test_a_user_scoped_session_is_never_captured(self):
        p = _provider({"turn", "session", "skill_usage"})
        p._scope_id = "owui:alice"
        self.assertEqual(_all_three(p), [])

    def test_a_skill_event_without_a_usage_row_still_empties_the_names_cache(self):
        p = _provider(())
        with patch.object(oracle_plugin, "_LOCAL_SKILLS_CACHE", {"x": (0.0, frozenset())}) as cache:
            p._on_skill_lifecycle_impl(action="used", skill_name="pdf")
            self.assertEqual(cache, {})  # a skill event still empties the local-names cache
        p._queue_background_insert.assert_not_called()

    def test_explicit_writes_do_not_depend_on_the_setting(self):
        p = _provider(())
        p._insert = MagicMock()
        p.on_memory_write("add", "memory", "The export runs nightly")
        p._insert.assert_called_once()


if __name__ == "__main__":
    unittest.main()
