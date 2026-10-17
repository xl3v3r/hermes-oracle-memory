import os
import sys
import unittest

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import tests.conftest
import __init__ as oracle_plugin

class TestFormatPrefetch(unittest.TestCase):
    def test_empty(self):
        provider = oracle_plugin.OracleMemoryProvider()
        self.assertEqual(provider._format_prefetch([]), "")
        self.assertEqual(provider._format_prefetch(None), "")

    def test_basic(self):
        provider = oracle_plugin.OracleMemoryProvider()
        rows = [
            {"content": "First memory", "target": "user", "agent_id": "agent1", "score": 0.95, "memory_id": "m1"},
            {"content": "Second memory", "target": "memory", "agent_id": "hermes", "score": 0.82, "memory_id": "m2"}
        ]
        expected = (
            "## Oracle 26ai Memory\n"
            '<retrieved_memory id="m1" target="user" score="0.95">First memory</retrieved_memory>\n'
            '<retrieved_memory id="m2" target="memory" score="0.82">Second memory</retrieved_memory>'
        )
        self.assertEqual(provider._format_prefetch(rows), expected)

    def test_defaults(self):
        provider = oracle_plugin.OracleMemoryProvider()
        rows = [{"content": "Minimal memory"}]
        expected = '## Oracle 26ai Memory\n<retrieved_memory id="" target="memory" score="0">Minimal memory</retrieved_memory>'
        self.assertEqual(provider._format_prefetch(rows), expected)

    def test_long_content(self):
        provider = oracle_plugin.OracleMemoryProvider()
        long_text = "A" * 300
        rows = [{"content": long_text}]
        expected_content = "A" * 277 + "..."
        expected = f'## Oracle 26ai Memory\n<retrieved_memory id="" target="memory" score="0">{expected_content}</retrieved_memory>'
        self.assertEqual(provider._format_prefetch(rows), expected)

    def test_removes_newlines(self):
        provider = oracle_plugin.OracleMemoryProvider()
        rows = [{"content": "Line 1\nLine 2\n\nLine 3"}]
        expected = '## Oracle 26ai Memory\n<retrieved_memory id="" target="memory" score="0">Line 1 Line 2  Line 3</retrieved_memory>'
        self.assertEqual(provider._format_prefetch(rows), expected)

    def test_limits_to_8_rows(self):
        provider = oracle_plugin.OracleMemoryProvider()
        rows = [{"content": f"Memory {i}"} for i in range(10)]
        result = provider._format_prefetch(rows)
        lines = result.split("\n")
        self.assertEqual(len(lines), 9)
        self.assertIn('<retrieved_memory id="" target="memory" score="0">Memory 7</retrieved_memory>', result)
        self.assertNotIn("Memory 8", result)

    def test_empty_content(self):
        provider = oracle_plugin.OracleMemoryProvider()
        rows = [{"content": None}, {"content": ""}]
        expected = (
            "## Oracle 26ai Memory\n"
            '<retrieved_memory id="" target="memory" score="0"></retrieved_memory>\n'
            '<retrieved_memory id="" target="memory" score="0"></retrieved_memory>'
        )
        self.assertEqual(provider._format_prefetch(rows), expected)

    def test_strips_control_characters(self):
        provider = oracle_plugin.OracleMemoryProvider()
        rows = [{"content": "Hello\x00World\x07!\x1b[31mRed\x7fDone"}]
        result = provider._format_prefetch(rows)
        self.assertIn("HelloWorld![31mRedDone", result)
        self.assertNotIn("\x00", result)
        self.assertNotIn("\x07", result)
        self.assertNotIn("\x1b", result)
        self.assertNotIn("\x7f", result)

    def test_html_escapes_attributes(self):
        provider = oracle_plugin.OracleMemoryProvider()
        rows = [{"content": "Escaped attrs", "memory_id": 'm"<>&', "target": "user'x"}]
        result = provider._format_prefetch(rows)
        self.assertIn('id="m&quot;&lt;&gt;&amp;" target="user&#x27;x"', result)


if __name__ == '__main__':
    unittest.main()
