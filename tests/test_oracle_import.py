import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import tests.conftest
import __init__ as oracle_plugin


class TestOracleChunkAndImport(unittest.TestCase):
    def setUp(self):
        self.provider = oracle_plugin.OracleMemoryProvider()
        self.provider._pool = MagicMock()
        self.provider._chunk_and_store = MagicMock(return_value=["chk_1", "chk_2"])

    def test_oracle_chunk_missing_params(self):
        res = self.provider.handle_tool_call("oracle_chunk", {})
        self.assertEqual(res, "error")  # tool_error returns "error" via conftest mock

    def test_oracle_chunk_with_content(self):
        res = self.provider.handle_tool_call("oracle_chunk", {"content": "Hello world text to chunk"})
        parsed = json.loads(res)
        self.assertEqual(parsed["result"], "chunked")
        self.assertEqual(parsed["count"], 2)
        self.assertEqual(parsed["memory_ids"], ["chk_1", "chk_2"])
        self.provider._chunk_and_store.assert_called_once_with("Hello world text to chunk", 80, 10)

    def test_oracle_chunk_with_file_path_text(self):
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".txt") as tf:
            tf.write("Line 1 from file.\nLine 2 from file.")
            tf_path = tf.name

        try:
            res = self.provider.handle_tool_call("oracle_chunk", {"file_path": tf_path, "max_words": 100})
            parsed = json.loads(res)
            self.assertEqual(parsed["result"], "chunked")
            self.provider._chunk_and_store.assert_called_once_with(
                "Line 1 from file.\nLine 2 from file.", 100, 10
            )
        finally:
            os.unlink(tf_path)

    def test_oracle_chunk_with_sharegpt_json(self):
        sample = {
            "conversations": [
                {"from": "human", "value": "How do I configure Oracle?"},
                {"from": "gpt", "value": "Use oracledb with wallet."},
            ]
        }
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json") as tf:
            json.dump(sample, tf)
            tf_path = tf.name

        try:
            res = self.provider.handle_tool_call("oracle_chunk", {"file_path": tf_path})
            parsed = json.loads(res)
            self.assertEqual(parsed["result"], "chunked")
            expected_text = "[HUMAN]: How do I configure Oracle?\n\n[GPT]: Use oracledb with wallet."
            self.provider._chunk_and_store.assert_called_once_with(expected_text, 80, 10)
        finally:
            os.unlink(tf_path)

    def test_oracle_chunk_with_multisession_sharegpt(self):
        sample = [
            {
                "conversations": [
                    {"from": "user", "value": "Turn 1"},
                    {"from": "assistant", "value": "Turn 2"},
                ]
            }
        ]
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json") as tf:
            json.dump(sample, tf)
            tf_path = tf.name

        try:
            res = self.provider.handle_tool_call("oracle_chunk", {"file_path": tf_path})
            parsed = json.loads(res)
            self.assertEqual(parsed["result"], "chunked")
            expected_text = "[USER]: Turn 1\n\n[ASSISTANT]: Turn 2"
            self.provider._chunk_and_store.assert_called_once_with(expected_text, 80, 10)
        finally:
            os.unlink(tf_path)

    def test_oracle_chunk_with_chat_messages_json(self):
        sample = [
            {
                "chat_messages": [
                    {"sender": "human", "text": "What is 2+2?"},
                    {"sender": "assistant", "text": "4"},
                ]
            }
        ]
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json") as tf:
            json.dump(sample, tf)
            tf_path = tf.name

        try:
            res = self.provider.handle_tool_call("oracle_chunk", {"file_path": tf_path})
            parsed = json.loads(res)
            self.assertEqual(parsed["result"], "chunked")
            expected_text = "[HUMAN]: What is 2+2?\n\n[ASSISTANT]: 4"
            self.provider._chunk_and_store.assert_called_once_with(expected_text, 80, 10)
        finally:
            os.unlink(tf_path)

    def test_oracle_chunk_nonexistent_file(self):
        res = self.provider.handle_tool_call("oracle_chunk", {"file_path": "/nonexistent/path/doc.txt"})
        self.assertEqual(res, "error")

    def test_oracle_chunk_uninitialized_pool(self):
        self.provider._pool = None
        res = self.provider.handle_tool_call("oracle_chunk", {"content": "Some text"})
        parsed = json.loads(res)
        self.assertIn("error", parsed)

    def test_register_with_skill_support(self):
        mock_ctx = MagicMock()
        oracle_plugin.register(mock_ctx)
        mock_ctx.register_memory_provider.assert_called_once()
        mock_ctx.register_skill.assert_called_once()
        args, _ = mock_ctx.register_skill.call_args
        self.assertEqual(args[0], "oracle-import")
        self.assertTrue(args[1].exists())

    def test_register_without_skill_support(self):
        class MinimalContext:
            def register_memory_provider(self, provider):
                self.provider = provider

        ctx = MinimalContext()
        # Should not raise AttributeError: MinimalContext has no attribute 'register_skill'
        oracle_plugin.register(ctx)
        self.assertIsInstance(ctx.provider, oracle_plugin.OracleMemoryProvider)


if __name__ == "__main__":
    unittest.main()
