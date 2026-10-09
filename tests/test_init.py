import os
import sys
import unittest
from unittest.mock import MagicMock

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

# Use conftest to mock dependencies properly
import tests.conftest
from __init__ import _is_internal_gateway_turn, OracleMemoryProvider


class TestInternalGatewayTurn(unittest.TestCase):

    def test_async_delegation_batch_complete(self):
        self.assertTrue(_is_internal_gateway_turn("[ASYNC DELEGATION BATCH COMPLETE 123]"))
        self.assertTrue(_is_internal_gateway_turn("[ASYNC DELEGATION COMPLETE abc]"))
        self.assertTrue(_is_internal_gateway_turn("[ASYNC COMPLETE]"))

    def test_context_compaction_and_summary(self):
        self.assertTrue(_is_internal_gateway_turn("[CONTEXT COMPACTION DONE]"))
        self.assertTrue(_is_internal_gateway_turn("[CONTEXT SUMMARY]: Here is a summary"))
        self.assertTrue(_is_internal_gateway_turn("[CONTEXT SUMMARY]"))

    def test_prior_context(self):
        self.assertTrue(_is_internal_gateway_turn("[PRIOR CONTEXT 456]"))

    def test_preserved_task_list(self):
        self.assertTrue(_is_internal_gateway_turn("[Your active task list was preserved across context compression]"))

    def test_background_processes_and_subagents(self):
        self.assertTrue(_is_internal_gateway_turn("[IMPORTANT: Background process 99 matched watch pattern test]"))
        self.assertTrue(_is_internal_gateway_turn("A background fan-out of 3 subagent(s) you dispatched earlier has finished."))
        self.assertTrue(_is_internal_gateway_turn("A background subagent you dispatched earlier has finished."))

    def test_leading_whitespace_and_case_insensitivity(self):
        self.assertTrue(_is_internal_gateway_turn("   [ASYNC COMPLETE]"))
        self.assertTrue(_is_internal_gateway_turn("\t[context compaction done]"))
        self.assertTrue(_is_internal_gateway_turn("\n[PRIOR CONTEXT]"))

    def test_non_matching_strings(self):
        self.assertFalse(_is_internal_gateway_turn("This is a normal user message."))
        self.assertFalse(_is_internal_gateway_turn("ASYNC COMPLETE")) # Missing brackets
        self.assertFalse(_is_internal_gateway_turn("[ASYNC DELEGATION BATCH COMPLET]")) # Typo
        self.assertFalse(_is_internal_gateway_turn("[CONTEXT SUMMARY X]")) # Summary needs exact or colon match
        self.assertFalse(_is_internal_gateway_turn("A background fan-out of subagents finished.")) # Missing numbers/different wording

    def test_empty_and_none(self):
        self.assertFalse(_is_internal_gateway_turn(""))
        self.assertFalse(_is_internal_gateway_turn(None))


class TestOracleMemoryProviderConfigSchema(unittest.TestCase):

    def test_get_config_schema(self):
        provider = OracleMemoryProvider()
        schema = provider.get_config_schema()

        self.assertIsInstance(schema, list)
        self.assertEqual(len(schema), 5)

        expected_keys = {"key", "description", "secret", "required"}
        for item in schema:
            self.assertIsInstance(item, dict)
            self.assertTrue(expected_keys.issubset(item.keys()))
            self.assertIsInstance(item["key"], str)
            self.assertIsInstance(item["description"], str)
            self.assertIsInstance(item["secret"], bool)
            self.assertIsInstance(item["required"], bool)

            if item["key"] == "user":
                self.assertEqual(item["env_var"], "OCI_DB_USER")
                self.assertTrue(item["required"])
                self.assertTrue(item["secret"])
            elif item["key"] == "password":
                self.assertEqual(item["env_var"], "OCI_DB_PASSWORD")
                self.assertTrue(item["required"])
                self.assertTrue(item["secret"])
            elif item["key"] == "dsn":
                self.assertEqual(item["env_var"], "OCI_DB_DSN")
                self.assertTrue(item["required"])
                self.assertTrue(item["secret"])
            elif item["key"] == "table_name":
                self.assertEqual(item["env_var"], "HERMES_ORACLE_TABLE")
                self.assertEqual(item["default"], "hermes_agent_memory")
                self.assertFalse(item["required"])
                self.assertFalse(item["secret"])
            elif item["key"] == "onnx_model_name":
                self.assertEqual(item["env_var"], "HERMES_ORACLE_ONNX_MODEL")
                self.assertFalse(item["required"])
                self.assertFalse(item["secret"])

        keys = {item["key"] for item in schema}
        self.assertEqual(keys, {"user", "password", "dsn", "table_name", "onnx_model_name"})


if __name__ == '__main__':
    unittest.main()
