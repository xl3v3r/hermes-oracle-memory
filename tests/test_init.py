import unittest
import sys
from unittest.mock import MagicMock

# Use conftest to mock dependencies properly
import tests.conftest

# Now we can import the function we want to test
from __init__ import _is_internal_gateway_turn

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

if __name__ == '__main__':
    unittest.main()
