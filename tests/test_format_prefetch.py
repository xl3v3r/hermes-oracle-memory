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
            {"content": "First memory", "target": "user", "agent_id": "agent1", "score": 0.95},
            {"content": "Second memory", "target": "memory", "agent_id": "hermes", "score": 0.82}
        ]
        expected = "## Oracle 26ai Memory\n- [user|agent1|0.95] First memory\n- [memory|hermes|0.82] Second memory"
        self.assertEqual(provider._format_prefetch(rows), expected)

    def test_defaults(self):
        provider = oracle_plugin.OracleMemoryProvider()
        rows = [{"content": "Minimal memory"}]
        expected = "## Oracle 26ai Memory\n- [memory|hermes|0] Minimal memory"
        self.assertEqual(provider._format_prefetch(rows), expected)

    def test_long_content(self):
        provider = oracle_plugin.OracleMemoryProvider()
        long_text = "A" * 300
        rows = [{"content": long_text}]
        expected_content = "A" * 277 + "..."
        expected = f"## Oracle 26ai Memory\n- [memory|hermes|0] {expected_content}"
        self.assertEqual(provider._format_prefetch(rows), expected)

    def test_removes_newlines(self):
        provider = oracle_plugin.OracleMemoryProvider()
        rows = [{"content": "Line 1\nLine 2\n\nLine 3"}]
        expected = "## Oracle 26ai Memory\n- [memory|hermes|0] Line 1 Line 2  Line 3"
        self.assertEqual(provider._format_prefetch(rows), expected)

    def test_limits_to_8_rows(self):
        provider = oracle_plugin.OracleMemoryProvider()
        rows = [{"content": f"Memory {i}"} for i in range(10)]
        result = provider._format_prefetch(rows)
        lines = result.split("\n")
        self.assertEqual(len(lines), 9)
        self.assertIn("- [memory|hermes|0] Memory 7", result)
        self.assertNotIn("Memory 8", result)

    def test_empty_content(self):
        provider = oracle_plugin.OracleMemoryProvider()
        rows = [{"content": None}, {"content": ""}]
        expected = "## Oracle 26ai Memory\n- [memory|hermes|0] \n- [memory|hermes|0] "
        self.assertEqual(provider._format_prefetch(rows), expected)

if __name__ == '__main__':
    unittest.main()
