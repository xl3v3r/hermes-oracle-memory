import sys
from unittest.mock import MagicMock
import unittest

# Mock out agent and tools modules
class MockMemoryProvider:
    pass

agent_mock = MagicMock()
agent_mock.memory_provider = MagicMock()
agent_mock.memory_provider.MemoryProvider = MockMemoryProvider
agent_mock.memory_provider.RecallStatus = MagicMock()
agent_mock.memory_provider.is_trivial_prompt = MagicMock()
agent_mock.secret_scope = MagicMock()
agent_mock.secret_scope.get_secret = MagicMock()

sys.modules['agent'] = agent_mock
sys.modules['agent.memory_provider'] = agent_mock.memory_provider
sys.modules['agent.secret_scope'] = agent_mock.secret_scope

tools_mock = MagicMock()
tools_mock.registry = MagicMock()
tools_mock.registry.tool_error = MagicMock()
sys.modules['tools'] = tools_mock
sys.modules['tools.registry'] = tools_mock.registry

import importlib.util
import os
plugin_path = os.path.abspath("__init__.py")
spec = importlib.util.spec_from_file_location("oracle_plugin", plugin_path)
oracle_plugin = importlib.util.module_from_spec(spec)
sys.modules["oracle_plugin"] = oracle_plugin
spec.loader.exec_module(oracle_plugin)

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
