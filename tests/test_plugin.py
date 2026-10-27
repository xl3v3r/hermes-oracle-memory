import os
import sys
import unittest
from unittest.mock import MagicMock, patch

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

        expected_names = {"oracle_search", "oracle_add", "oracle_update", "oracle_delete", "oracle_chunk", "oracle_sync_skills"}
        actual_names = {schema["name"] for schema in schemas}
        self.assertEqual(actual_names, expected_names)

    def test_unavailable_reason_no_oracledb(self):
        provider = oracle_plugin.OracleMemoryProvider()
        with patch.dict('sys.modules', {'oracledb': None}):
            with patch('builtins.__import__') as mock_import:
                def mock_import_func(name, *args, **kwargs):
                    if name == 'oracledb':
                        raise ImportError("No module named 'oracledb'")
                    return __import__(name, *args, **kwargs)
                mock_import.side_effect = mock_import_func

                self.assertEqual(provider.unavailable_reason(), "oracledb is not installed in the Hermes venv")

    def test_unavailable_reason_missing_one_secret(self):
        provider = oracle_plugin.OracleMemoryProvider()
        mock_oracledb = MagicMock()
        with patch.dict('sys.modules', {'oracledb': mock_oracledb}):
            def mock_get_secret(key, default=None):
                secrets = {
                    "OCI_DB_USER": "user",
                    "OCI_DB_DSN": "dsn",
                }
                return secrets.get(key)

            with patch.object(oracle_plugin, 'get_secret', side_effect=mock_get_secret):
                self.assertEqual(provider.unavailable_reason(), "missing env: OCI_DB_PASSWORD")

    def test_unavailable_reason_missing_multiple_secrets(self):
        provider = oracle_plugin.OracleMemoryProvider()
        mock_oracledb = MagicMock()
        with patch.dict('sys.modules', {'oracledb': mock_oracledb}):
            def mock_get_secret(key, default=None):
                secrets = {
                    "OCI_DB_USER": "user",
                }
                return secrets.get(key)

            with patch.object(oracle_plugin, 'get_secret', side_effect=mock_get_secret):
                self.assertEqual(provider.unavailable_reason(), "missing env: OCI_DB_PASSWORD, OCI_DB_DSN")

    def test_unavailable_reason_missing_all_secrets(self):
        provider = oracle_plugin.OracleMemoryProvider()
        mock_oracledb = MagicMock()
        with patch.dict('sys.modules', {'oracledb': mock_oracledb}):
            with patch.object(oracle_plugin, 'get_secret', return_value=None):
                self.assertEqual(provider.unavailable_reason(), "missing env: OCI_DB_USER, OCI_DB_PASSWORD, OCI_DB_DSN")

    def test_unavailable_reason_all_present(self):
        provider = oracle_plugin.OracleMemoryProvider()
        mock_oracledb = MagicMock()
        with patch.dict('sys.modules', {'oracledb': mock_oracledb}):
            with patch.object(oracle_plugin, 'get_secret', return_value="secret_val"):
                self.assertEqual(provider.unavailable_reason(), "")


class TestSkillLifecycleHook(unittest.TestCase):
    def test_register_hooks_on_skill_lifecycle(self):
        mock_ctx = MagicMock()
        oracle_plugin.register(mock_ctx)
        mock_ctx.register_memory_provider.assert_called_once()
        mock_ctx.register_hook.assert_called_with("on_skill_lifecycle", unittest.mock.ANY)
        cb = mock_ctx.register_hook.call_args[0][1]
        self.assertTrue(callable(cb))

    def test_on_skill_lifecycle_used_action(self):
        mock_ctx = MagicMock()
        oracle_plugin.register(mock_ctx)
        provider = mock_ctx.register_memory_provider.call_args[0][0]
        hook_cb = mock_ctx.register_hook.call_args[0][1]

        with patch.object(provider, "_queue_background_insert") as mock_insert:
            hook_cb(action="used", skill_name="oracle-import", session_id="test-session-123")

            mock_insert.assert_called_once()
            kwargs = mock_insert.call_args[1]
            self.assertTrue(kwargs["memory_id"].startswith("skill_"))
            self.assertEqual(kwargs["session_id"], "test-session-123")
            self.assertEqual(kwargs["role"], "fact")
            self.assertEqual(kwargs["target"], "memory")
            self.assertEqual(
                kwargs["metadata"],
                {"kind": "skill_usage", "skill": "oracle-import", "action": "used"},
            )
            content = kwargs["content"]
            self.assertIn("[skill_usage]", content)
            self.assertIn("[skill:oracle-import]", content)
            self.assertIn("[action:used]", content)
            self.assertIn("[session:test-session-123]", content)
            self.assertIn("Skill 'oracle-import' activated.", content)
            self.assertLess(len(content), 200)

    def test_on_skill_lifecycle_loaded_action(self):
        mock_ctx = MagicMock()
        oracle_plugin.register(mock_ctx)
        provider = mock_ctx.register_memory_provider.call_args[0][0]
        hook_cb = mock_ctx.register_hook.call_args[0][1]

        with patch.object(provider, "_queue_background_insert") as mock_insert:
            hook_cb(
                action="loaded",
                skill_name="test-skill",
                session_id="session-xyz",
                ts="2026-10-18T07:49:40Z",
            )

            mock_insert.assert_called_once()
            kwargs = mock_insert.call_args[1]
            expected_content = (
                "[skill_usage] [skill:test-skill] [action:loaded] [ts:2026-10-18T07:49:40Z] "
                "[session:session-xyz] Skill 'test-skill' activated."
            )
            self.assertEqual(kwargs["content"], expected_content)
            self.assertLess(len(kwargs["content"]), 200)
            self.assertEqual(kwargs["role"], "fact")
            self.assertEqual(kwargs["session_id"], "session-xyz")
            self.assertEqual(kwargs["target"], "memory")
            self.assertEqual(
                kwargs["metadata"],
                {"kind": "skill_usage", "skill": "test-skill", "action": "loaded"},
            )

    def test_on_skill_lifecycle_ignored_actions(self):
        mock_ctx = MagicMock()
        oracle_plugin.register(mock_ctx)
        provider = mock_ctx.register_memory_provider.call_args[0][0]
        hook_cb = mock_ctx.register_hook.call_args[0][1]

        for ignored in ("created", "archived", "patched", "edited", "unknown"):
            with patch.object(provider, "_queue_background_insert") as mock_insert:
                hook_cb(action=ignored, skill_name="test-skill", session_id="s1")
                mock_insert.assert_not_called()

    def test_on_skill_lifecycle_empty_skill_name(self):
        mock_ctx = MagicMock()
        oracle_plugin.register(mock_ctx)
        provider = mock_ctx.register_memory_provider.call_args[0][0]
        hook_cb = mock_ctx.register_hook.call_args[0][1]

        with patch.object(provider, "_queue_background_insert") as mock_insert:
            hook_cb(action="used", skill_name="", session_id="s1")
            mock_insert.assert_not_called()

    def test_on_skill_lifecycle_failsafe_on_exception(self):
        mock_ctx = MagicMock()
        oracle_plugin.register(mock_ctx)
        provider = mock_ctx.register_memory_provider.call_args[0][0]
        hook_cb = mock_ctx.register_hook.call_args[0][1]

        with patch.object(provider, "_queue_background_insert", side_effect=RuntimeError("Insert failed")):
            # Should not raise
            try:
                hook_cb(action="used", skill_name="test-skill", session_id="s1")
            except Exception as e:
                self.fail(f"on_skill_lifecycle raised an exception unexpectedly: {e}")

    def test_on_skill_lifecycle_dict_argument(self):
        mock_ctx = MagicMock()
        oracle_plugin.register(mock_ctx)
        provider = mock_ctx.register_memory_provider.call_args[0][0]
        hook_cb = mock_ctx.register_hook.call_args[0][1]

        with patch.object(provider, "_queue_background_insert") as mock_insert:
            hook_cb({"action": "used", "skill_name": "test-skill", "session_id": "sess-dict"})
            mock_insert.assert_called_once()
            kwargs = mock_insert.call_args[1]
            self.assertEqual(kwargs["session_id"], "sess-dict")
            self.assertIn("[skill:test-skill]", kwargs["content"])

    def test_on_skill_lifecycle_session_id_fallback(self):
        mock_ctx = MagicMock()
        oracle_plugin.register(mock_ctx)
        provider = mock_ctx.register_memory_provider.call_args[0][0]
        provider._session_id = "provider-session-default"
        hook_cb = mock_ctx.register_hook.call_args[0][1]

        with patch.object(provider, "_queue_background_insert") as mock_insert:
            hook_cb(action="used", skill_name="test-skill")
            mock_insert.assert_called_once()
            kwargs = mock_insert.call_args[1]
            self.assertEqual(kwargs["session_id"], "provider-session-default")
            self.assertIn("[session:provider-session-default]", kwargs["content"])


if __name__ == '__main__':
    unittest.main()
