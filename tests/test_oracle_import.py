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

scripts_dir = os.path.join(root_dir, "skills", "oracle-import", "scripts")
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

import tests.conftest
import __init__ as oracle_plugin
import import_conversations


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
            expected_turns = [
                {"sender": "human", "text": "How do I configure Oracle?", "conv_ref": ""},
                {"sender": "gpt", "text": "Use oracledb with wallet.", "conv_ref": ""},
            ]
            self.provider._chunk_and_store.assert_called_once_with("", 80, 10, turns=expected_turns)
        finally:
            os.unlink(tf_path)

    def test_oracle_chunk_with_oracle_import_json_format(self):
        sample = {
            "conversations": [
                {
                    "from": "system",
                    "value": "Conversation: ITAHM acronym meaning\n\nSummary: Overview of abbreviations\n\nUUID: 6912",
                }
            ]
        }
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json") as tf:
            json.dump(sample, tf)
            tf_path = tf.name

        try:
            res = self.provider.handle_tool_call("oracle_chunk", {"file_path": tf_path})
            parsed = json.loads(res)
            self.assertEqual(parsed["result"], "chunked")
            expected_turns = [
                {
                    "sender": "system",
                    "text": "Conversation: ITAHM acronym meaning\n\nSummary: Overview of abbreviations\n\nUUID: 6912",
                    "conv_ref": "",
                }
            ]
            self.provider._chunk_and_store.assert_called_once_with("", 80, 10, turns=expected_turns)
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
            expected_turns = [
                {"sender": "user", "text": "Turn 1", "conv_ref": ""},
                {"sender": "assistant", "text": "Turn 2", "conv_ref": ""},
            ]
            self.provider._chunk_and_store.assert_called_once_with("", 80, 10, turns=expected_turns)
        finally:
            os.unlink(tf_path)

    def test_oracle_chunk_with_chat_messages_structured_content(self):
        sample = [
            {
                "name": "Session Alpha",
                "summary": "Alpha summary",
                "chat_messages": [
                    {"sender": "human", "text": "What is 2+2?"},
                    {
                        "sender": "assistant",
                        "text": "",
                        "content": [
                            {"type": "thinking", "thinking": "Internal thought"},
                            {"type": "text", "text": "The answer is 4."},
                        ],
                    },
                ],
            }
        ]
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json") as tf:
            json.dump(sample, tf)
            tf_path = tf.name

        try:
            res = self.provider.handle_tool_call("oracle_chunk", {"file_path": tf_path})
            parsed = json.loads(res)
            self.assertEqual(parsed["result"], "chunked")
            # Should include conversation header and clean assistant text as
            # ordered turns stamped with the conversation title as conv_ref.
            turns = self.provider._chunk_and_store.call_args[1]["turns"]
            texts = [t["text"] for t in turns]
            self.assertIn("Conversation: Session Alpha", texts[0])
            self.assertIn("Summary: Alpha summary", texts[0])
            self.assertEqual(turns[1]["text"], "What is 2+2?")
            self.assertEqual(turns[2]["text"], "The answer is 4.")
            self.assertNotIn("Internal thought", " ".join(texts))
            for t in turns:
                self.assertEqual(t["conv_ref"], "Session Alpha")
        finally:
            os.unlink(tf_path)

    def test_oracle_chunk_clob_binding(self):
        # Un-mock _chunk_and_store to verify setinputsizes is called
        provider = oracle_plugin.OracleMemoryProvider()
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_pool.acquire.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        provider._pool = mock_pool

        # Table execute yields chunks
        mock_cur.__iter__.return_value = [("Chunk 1",)]

        provider._insert_many = MagicMock()
        ids = provider._chunk_and_store("Short text", 80, 10)
        self.assertEqual(len(ids), 1)
        mock_cur.setinputsizes.assert_called_once()

    def test_oracle_chunk_nonexistent_file(self):
        res = self.provider.handle_tool_call("oracle_chunk", {"file_path": "/nonexistent/path/doc.txt"})
        self.assertEqual(res, "error")

    def test_oracle_chunk_uninitialized_pool(self):
        self.provider._pool = None
        res = self.provider.handle_tool_call("oracle_chunk", {"content": "Some text"})
        parsed = json.loads(res)
        self.assertIn("error", parsed)

    def test_import_conversations_parses_oracle_import_json(self):
        sample = {
            "conversations": [
                {
                    "from": "system",
                    "value": "Conversation: ITAHM\n\nSummary: Overview\n\nUUID: abc-123",
                }
            ]
        }
        turns = import_conversations.parse_conversations(sample)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["role"], "session")
        self.assertEqual(turns[0]["sender"], "system")
        self.assertIn("Conversation: ITAHM", turns[0]["content"])

    def test_import_conversations_parses_claude_export(self):
        sample = [
            {
                "name": "Title",
                "summary": "Summary text",
                "chat_messages": [
                    {"sender": "human", "text": "Question"},
                    {
                        "sender": "assistant",
                        "text": "",
                        "content": [
                            {"type": "thinking", "thinking": "scratchpad"},
                            {"type": "text", "text": "Answer"},
                        ],
                    },
                ],
            }
        ]
        turns = import_conversations.parse_conversations(sample)
        self.assertEqual(len(turns), 3)  # summary + question + answer
        self.assertEqual(turns[0]["role"], "session")
        self.assertIn("[CONVERSATION]: Title", turns[0]["content"])
        self.assertEqual(turns[1]["role"], "user")
        self.assertEqual(turns[2]["role"], "assistant")
        self.assertEqual(turns[2]["content"], "[ASSISTANT]: Answer")

    def test_import_conversations_safe_truncated_json_decode(self):
        # A valid object followed by truncated text
        raw = '[{"name": "Session 1", "summary": "Sum 1"}, {"name": "Session 2", "sum'
        recovered = import_conversations.parse_json_safely(raw)
        self.assertEqual(len(recovered), 1)
        turns = import_conversations.parse_conversations(recovered[0])
        self.assertEqual(len(turns), 1)
        self.assertIn("Session 1", turns[0]["content"])

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
        oracle_plugin.register(ctx)
        self.assertIsInstance(ctx.provider, oracle_plugin.OracleMemoryProvider)


if __name__ == "__main__":
    unittest.main()
