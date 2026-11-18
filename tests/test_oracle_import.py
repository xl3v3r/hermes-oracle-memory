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
from scripts import import_conversations_ordered as importer


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

    def test_register_with_skill_support(self):
        mock_ctx = MagicMock()
        oracle_plugin.register(mock_ctx)
        mock_ctx.register_memory_provider.assert_called_once()
        self.assertEqual(mock_ctx.register_skill.call_count, 2)
        registered_skills = {call[0][0]: call[0][1] for call in mock_ctx.register_skill.call_args_list}
        self.assertIn("oracle-import", registered_skills)
        self.assertTrue(registered_skills["oracle-import"].exists())
        self.assertIn("oracle-skill-corpus", registered_skills)
        self.assertTrue(registered_skills["oracle-skill-corpus"].exists())

    def test_register_without_skill_support(self):
        class MinimalContext:
            def register_memory_provider(self, provider):
                self.provider = provider

        ctx = MinimalContext()
        oracle_plugin.register(ctx)
        self.assertIsInstance(ctx.provider, oracle_plugin.OracleMemoryProvider)


CLAUDE_EXPORT = [
    {
        "name": "Relay outage",
        "summary": "Why the harbor relay dropped connections",
        "uuid": "c-1",
        "chat_messages": [
            {"sender": "human", "text": "Why did the relay drop?", "created_at": "2026-01-02T10:00:00Z", "uuid": "m-1"},
            {
                "sender": "assistant",
                "text": "",
                "content": [
                    {"type": "thinking", "thinking": "scratchpad"},
                    {"type": "text", "text": "The quartz certificate expired."},
                ],
                "created_at": "2026-01-02T10:00:05Z",
                "uuid": "m-2",
            },
        ],
    }
]


def _junk_roles():
    """The roles every search excludes (parsed from the provider's filter)."""
    import re

    m = re.search(r"NOT IN \(([^)]*)\)", oracle_plugin.OracleMemoryProvider._JUNK)
    assert m is not None
    return {r.strip().strip("'") for r in m.group(1).split(",")}


class TestConversationImporter(unittest.TestCase):
    """#20: one importer. Everything it stores can be found by search."""

    def test_there_is_one_importer(self):
        repo = Path(root_dir)
        found = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("import_conversations*.py")
                       if ".git/" not in p.as_posix())
        self.assertEqual(found, ["scripts/import_conversations_ordered.py"])
        skill = (repo / "skills" / "oracle-import" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("scripts/import_conversations_ordered.py", skill)
        self.assertNotIn("skills/oracle-import/scripts", skill)

    def test_claude_export_keeps_order_speakers_and_the_summary(self):
        convs = importer.parse_conversations(CLAUDE_EXPORT)
        self.assertEqual(len(convs), 1)
        turns = convs[0]["turns"]
        self.assertEqual([t["sender"] for t in turns], ["system", "human", "assistant"])
        self.assertIn("Relay outage", turns[0]["text"])
        self.assertIn("Why the harbor relay dropped connections", turns[0]["text"])
        self.assertEqual(turns[2]["text"], "The quartz certificate expired.")  # thinking block dropped
        self.assertEqual(convs[0]["conv_ref"], "Relay outage")

    def test_messages_are_ordered_by_time_not_file_position(self):
        export = [{"name": "t", "chat_messages": [
            {"sender": "assistant", "text": "second", "created_at": "2026-01-02T10:00:05Z"},
            {"sender": "human", "text": "first", "created_at": "2026-01-02T10:00:00Z"},
        ]}]
        turns = importer.parse_conversations(export)[0]["turns"]
        self.assertEqual([t["text"] for t in turns if t["sender"] != "system"], ["first", "second"])

    def test_sharegpt(self):
        convs = importer.parse_conversations({"conversations": [
            {"from": "human", "value": "Q"}, {"from": "gpt", "value": "A"},
        ]})
        self.assertEqual([(t["sender"], t["text"]) for t in convs[0]["turns"]], [("human", "Q"), ("gpt", "A")])

    def test_summary_export_gives_each_entry_its_own_document(self):
        # {"conversations": [...]} of system entries is a list of summaries,
        # not one dialogue. One shared conv_ref would let a search return at
        # most two chunks of the whole import (the per-source cap).
        convs = importer.parse_conversations({"conversations": [
            {"from": "system", "value": "Conversation: ITAHM\n\nSummary: Overview\n\nUUID: abc-123"},
            {"from": "system", "value": "Conversation: Relay\n\nSummary: Outage"},
            {"from": "system", "value": "An entry without a title line"},
        ]})
        self.assertEqual([c["conv_ref"] for c in convs], ["ITAHM", "Relay", "conversation-00002"])
        self.assertTrue(all(len(c["turns"]) == 1 for c in convs))

    def test_list_of_sharegpt_sessions(self):
        convs = importer.parse_conversations([
            {"conversations": [{"from": "human", "value": "Q1"}, {"from": "gpt", "value": "A1"}]},
            {"title": "Second", "conversations": [{"from": "human", "value": "Q2"}]},
            "a bare string entry",
        ])
        self.assertEqual([c["conv_ref"] for c in convs], ["conv0000", "Second", "conv0002"])
        self.assertEqual([t["text"] for t in convs[0]["turns"]], ["Q1", "A1"])
        self.assertEqual([t["sender"] for t in convs[1]["turns"]], ["system", "human"])

    def test_undated_messages_keep_file_order_after_dated_ones(self):
        export = [{"chat_messages": [
            {"sender": "human", "text": "undated a"},
            {"sender": "human", "text": "dated", "created_at": "2026-01-01T00:00:00Z"},
            {"sender": "human", "text": "undated b"},
        ]}]
        self.assertEqual([t["text"] for t in importer.parse_conversations(export)[0]["turns"]],
                         ["dated", "undated a", "undated b"])

    def test_empty_and_unknown_shapes(self):
        for data in (None, 5, "text", [], {}, {"chat_messages": []}, [{"chat_messages": [{"sender": "human", "text": " "}]}]):
            self.assertEqual(importer.parse_conversations(data), [], repr(data))

    def test_session_with_only_a_title_and_summary_is_kept(self):
        convs = importer.parse_conversations([{"name": "Session 1", "summary": "Sum 1"}])
        self.assertEqual(len(convs), 1)
        self.assertIn("Session 1", convs[0]["turns"][0]["text"])
        self.assertIn("Sum 1", convs[0]["turns"][0]["text"])

    def test_truncated_json_imports_the_complete_leading_objects(self):
        raw = '[{"name": "Session 1", "summary": "Sum 1"}, {"name": "Session 2", "sum'
        self.assertEqual(importer.parse_json_safely(raw), [{"name": "Session 1", "summary": "Sum 1"}])
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "cut.json"
            f.write_text(raw, encoding="utf-8")
            convs = importer.load_archive(f)
        self.assertEqual([c["conv_ref"] for c in convs], ["Session 1"])

    def test_complete_json_is_parsed_as_one_value(self):
        self.assertEqual(importer.parse_json_safely('{"a": 1}'), [{"a": 1}])
        self.assertEqual(importer.parse_json_safely("not json at all"), [])

    def test_zip_archive(self):
        import zipfile

        with tempfile.TemporaryDirectory() as td:
            z = Path(td) / "export.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("users.json", "[]")
                zf.writestr("conversations.json", json.dumps(CLAUDE_EXPORT))
            convs = importer.load_archive(z)
        self.assertEqual(len(convs), 1)

    def _run_import(self, argv, export=CLAUDE_EXPORT):
        """Run the importer with a real provider object whose inserts are captured."""
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        stored = []

        def initialize(session_id, **kw):
            provider._session_id = session_id

        plugin = MagicMock()
        plugin.OracleMemoryProvider.return_value = provider
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "conversations.json"
            f.write_text(json.dumps(export), encoding="utf-8")
            with patch.object(importer.common, "load_plugin", return_value=plugin), \
                    patch.object(provider, "initialize", side_effect=initialize), \
                    patch.object(provider, "shutdown") as shutdown, \
                    patch.object(provider, "_chunk_text", side_effect=lambda text, mw, ov: [text]), \
                    patch.object(provider, "_insert_many", side_effect=lambda rows: stored.extend(rows) or []):
                rc = importer.main([str(f), *argv])
        return rc, stored, shutdown

    def test_every_imported_row_can_be_found_by_search(self):
        rc, rows, shutdown = self._run_import(["--session-id", "archive-1"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(rows), 3)  # summary, question, answer
        junk = _junk_roles()
        self.assertTrue({"turn", "assistant", "session"} <= junk)
        for row in rows:
            self.assertNotIn(row["role"], junk, row)
            self.assertEqual(row["role"], "chunk")
            self.assertEqual(row["target"], "memory")
            self.assertEqual(row["session_id"], "archive-1")
        shutdown.assert_called_once()

    def test_the_assistant_message_is_stored_with_its_speaker_and_position(self):
        _, rows, _ = self._run_import([])
        answer = next(r for r in rows if "quartz certificate" in r["content"])
        self.assertEqual(answer["metadata"]["sender"], "assistant")
        self.assertEqual(answer["conv_ref"], "Relay outage")
        self.assertEqual((answer["turn_index"], answer["chunk_index"]), (2, 0))
        self.assertRegex(answer["session_id"], r"^import-ordered-\d{8}$")

    def test_provider_is_shut_down_when_an_insert_fails(self):
        provider = oracle_plugin.OracleMemoryProvider()
        provider._pool = MagicMock()
        plugin = MagicMock()
        plugin.OracleMemoryProvider.return_value = provider
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "c.json"
            f.write_text(json.dumps(CLAUDE_EXPORT), encoding="utf-8")
            with patch.object(importer.common, "load_plugin", return_value=plugin), \
                    patch.object(provider, "initialize"), patch.object(provider, "shutdown") as shutdown, \
                    patch.object(provider, "_chunk_and_store", side_effect=RuntimeError("ORA-00600")):
                with self.assertRaises(RuntimeError):
                    importer.run(importer.argparse.Namespace(
                        file=str(f), env_file=None, limit_conversations=0, session_id="s", max_words=80, overlap=10))
        shutdown.assert_called_once()

    def test_missing_file_needs_neither_plugin_nor_database(self):
        with patch.object(importer.common, "load_plugin", side_effect=AssertionError("must not load")):
            self.assertEqual(importer.main(["/nonexistent/path/file.json"]), 2)

    def test_archive_without_conversations_needs_neither_plugin_nor_database(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "empty.json"
            f.write_text("[]", encoding="utf-8")
            with patch.object(importer.common, "load_plugin", side_effect=AssertionError("must not load")):
                self.assertEqual(importer.main(["--file", str(f)]), 2)

    def test_the_archive_is_named_once(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            importer.main([])
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            importer.main(["a.json", "--file", "b.json"])


if __name__ == "__main__":
    unittest.main()

