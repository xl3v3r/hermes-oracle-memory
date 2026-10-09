import os
import sys
import unittest

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import tests.conftest
import __init__ as oracle_plugin

class TestOracleMemoryProvider(unittest.TestCase):
    def test_get_tool_schemas(self):
        provider = oracle_plugin.OracleMemoryProvider()
        schemas = provider.get_tool_schemas()

        self.assertIsInstance(schemas, list)
        self.assertEqual(len(schemas), len(oracle_plugin.TOOL_SCHEMAS))
        self.assertEqual(schemas, oracle_plugin.TOOL_SCHEMAS)
        # Verify we get a new list (to prevent accidental mutation of the original)
        self.assertIsNot(schemas, oracle_plugin.TOOL_SCHEMAS)

    def test_get_tool_schemas_content(self):
        provider = oracle_plugin.OracleMemoryProvider()
        schemas = provider.get_tool_schemas()

        expected_names = {"oracle_search", "oracle_add", "oracle_update", "oracle_delete", "oracle_chunk"}
        actual_names = {schema["name"] for schema in schemas}
        self.assertEqual(actual_names, expected_names)

if __name__ == "__main__":
    unittest.main()
