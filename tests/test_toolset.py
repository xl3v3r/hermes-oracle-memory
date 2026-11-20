"""Tests for the #38 toolset: the plugin's own 'oracle' toolset, the registry
tool handlers and availability check, the memory-toolset gate mirror, and the
pre_llm_call / pre_tool_call hooks. No database."""

import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import tests.conftest  # noqa: E402,F401
import __init__ as oracle_plugin  # noqa: E402


def _install_hermes_cli(platform_toolsets=None, enabled=None, load_config_raises=False):
    """Fake hermes_cli.config.load_config + hermes_cli.tools_config._get_platform_tools
    so _memory_block_withheld can be exercised without a Hermes install."""
    config_mod = types.ModuleType("hermes_cli.config")
    tools_config_mod = types.ModuleType("hermes_cli.tools_config")

    def load_config():
        if load_config_raises:
            raise RuntimeError("no config")
        return {"platform_toolsets": platform_toolsets}

    def _get_platform_tools(config, platform, include_default_mcp_servers=True):
        return enabled

    config_mod.load_config = load_config
    tools_config_mod._get_platform_tools = _get_platform_tools

    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.config = config_mod
    hermes_cli.tools_config = tools_config_mod

    names = ("hermes_cli", "hermes_cli.config", "hermes_cli.tools_config")
    saved = {name: sys.modules.get(name) for name in names}
    sys.modules["hermes_cli"] = hermes_cli
    sys.modules["hermes_cli.config"] = config_mod
    sys.modules["hermes_cli.tools_config"] = tools_config_mod
    return saved


def _restore_modules(saved):
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class TestToolsetRegistration(unittest.TestCase):
    def setUp(self):
        oracle_plugin._reset_registration_state()
        with oracle_plugin._LIVE_PROVIDERS_LOCK:
            oracle_plugin._LIVE_PROVIDERS.clear()

    def tearDown(self):
        oracle_plugin._reset_registration_state()
        with oracle_plugin._LIVE_PROVIDERS_LOCK:
            oracle_plugin._LIVE_PROVIDERS.clear()

    def test_register_tools_under_oracle_toolset(self):
        mock_ctx = MagicMock()
        oracle_plugin.register(mock_ctx)

        self.assertEqual(mock_ctx.register_tool.call_count, len(oracle_plugin.TOOL_SCHEMAS))
        expected_names = [s["name"] for s in oracle_plugin.TOOL_SCHEMAS]
        seen_names = [c.kwargs["name"] for c in mock_ctx.register_tool.call_args_list]
        self.assertEqual(seen_names, expected_names)

        schemas_by_name = {s["name"]: s for s in oracle_plugin.TOOL_SCHEMAS}
        for call in mock_ctx.register_tool.call_args_list:
            kwargs = call.kwargs
            self.assertEqual(kwargs["toolset"], "oracle")
            self.assertIs(kwargs["schema"], schemas_by_name[kwargs["name"]])
            self.assertTrue(callable(kwargs["handler"]), kwargs["name"])
            self.assertTrue(callable(kwargs["check_fn"]), kwargs["name"])

    def test_register_is_guarded_against_duplicate_registration(self):
        mock_ctx = MagicMock()
        oracle_plugin.register(mock_ctx)
        first_count = mock_ctx.register_tool.call_count
        oracle_plugin.register(MagicMock())
        self.assertEqual(mock_ctx.register_tool.call_count, first_count)


class TestRegistryToolHandler(unittest.TestCase):
    def setUp(self):
        oracle_plugin._reset_registration_state()
        with oracle_plugin._LIVE_PROVIDERS_LOCK:
            oracle_plugin._LIVE_PROVIDERS.clear()

    def tearDown(self):
        oracle_plugin._reset_registration_state()
        with oracle_plugin._LIVE_PROVIDERS_LOCK:
            oracle_plugin._LIVE_PROVIDERS.clear()

    def _live(self, sid):
        p = oracle_plugin.OracleMemoryProvider()
        p._pool = MagicMock()
        p._initialized = True
        p._session_id = sid
        oracle_plugin._register_live_provider(p)
        return p

    def test_routes_to_live_provider_of_the_session(self):
        live = self._live("sess-A")
        live.handle_tool_call = MagicMock(return_value='{"ok": true}')
        handler = oracle_plugin._make_registry_tool_handler("oracle_search")
        result = handler({"query": "x"}, session_id="sess-A")
        self.assertEqual(result, '{"ok": true}')
        live.handle_tool_call.assert_called_once_with("oracle_search", {"query": "x"})

    def test_no_provider_returns_not_available_json(self):
        handler = oracle_plugin._make_registry_tool_handler("oracle_add")
        result = json.loads(handler({"content": "x"}, session_id="nobody"))
        self.assertIn("error", result)
        self.assertIn("not available", result["error"])


class TestRegistryToolCheckFn(unittest.TestCase):
    def tearDown(self):
        oracle_plugin._reset_registration_state()

    def test_false_when_no_registry_provider(self):
        oracle_plugin._REGISTRY_PROVIDER = None
        self.assertFalse(oracle_plugin._registry_tool_check_fn())

    def test_tracks_provider_availability(self):
        provider = MagicMock()
        oracle_plugin._REGISTRY_PROVIDER = provider
        provider.is_available.return_value = True
        self.assertTrue(oracle_plugin._registry_tool_check_fn())
        provider.is_available.return_value = False
        self.assertFalse(oracle_plugin._registry_tool_check_fn())


class TestMemoryBlockWithheld(unittest.TestCase):
    def test_true_when_memory_absent(self):
        saved = _install_hermes_cli(
            platform_toolsets={"cli": ["files", "web"]},
            enabled=["files", "web"],
        )
        try:
            self.assertTrue(oracle_plugin._memory_block_withheld("cli"))
        finally:
            _restore_modules(saved)

    def test_false_when_memory_present(self):
        saved = _install_hermes_cli(
            platform_toolsets={"cli": ["memory", "files"]},
            enabled=["memory", "files"],
        )
        try:
            self.assertFalse(oracle_plugin._memory_block_withheld("cli"))
        finally:
            _restore_modules(saved)

    def test_false_when_platform_has_no_saved_list(self):
        saved = _install_hermes_cli(platform_toolsets={}, enabled=[])
        try:
            self.assertFalse(oracle_plugin._memory_block_withheld("cli"))
        finally:
            _restore_modules(saved)

    def test_desktop_resolves_as_cli(self):
        saved = _install_hermes_cli(
            platform_toolsets={"cli": ["files"]},
            enabled=["files"],
        )
        try:
            self.assertTrue(oracle_plugin._memory_block_withheld("desktop"))
        finally:
            _restore_modules(saved)

    def test_false_on_exception(self):
        saved = _install_hermes_cli(load_config_raises=True)
        try:
            self.assertFalse(oracle_plugin._memory_block_withheld("cli"))
        finally:
            _restore_modules(saved)


class TestPreLlmCallHook(unittest.TestCase):
    def _provider(self):
        p = oracle_plugin.OracleMemoryProvider()
        p._tuning = {"optional_rules": frozenset()}
        return p

    def test_none_when_not_first_turn(self):
        p = self._provider()
        with patch.object(oracle_plugin, "_memory_block_withheld", return_value=True):
            self.assertIsNone(p.pre_llm_call(platform="cli", is_first_turn=False))

    def test_none_when_block_present(self):
        p = self._provider()
        with patch.object(oracle_plugin, "_memory_block_withheld", return_value=False):
            self.assertIsNone(p.pre_llm_call(platform="cli", is_first_turn=True))

    def test_injects_rules_when_withheld_on_first_turn(self):
        p = self._provider()
        with patch.object(oracle_plugin, "_memory_block_withheld", return_value=True):
            result = p.pre_llm_call(platform="cli", is_first_turn=True)
        self.assertIsInstance(result, dict)
        self.assertIn("context", result)
        self.assertIn("# Oracle 26ai Memory Rules", result["context"])
        self.assertIn("'memory' toolset", result["context"])


class TestPreToolCallHook(unittest.TestCase):
    def test_none_without_oracle_only(self):
        p = oracle_plugin.OracleMemoryProvider()
        p._tuning = {"optional_rules": frozenset()}
        self.assertIsNone(p.pre_tool_call(tool_name="memory", args={}))

    def _oracle_only_provider(self):
        p = oracle_plugin.OracleMemoryProvider()
        p._tuning = {"optional_rules": frozenset({"oracle_only"})}
        return p

    def test_blocks_builtin_memory_tool(self):
        p = self._oracle_only_provider()
        result = p.pre_tool_call(tool_name="memory", args={})
        self.assertEqual(result["action"], "block")
        self.assertIn("HERMES_ORACLE_OPTIONAL_RULES", result["message"])

    def test_blocks_write_file_on_memory_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "hermes"
            (home / "memories").mkdir(parents=True)
            p = self._oracle_only_provider()
            p._hermes_home = home
            result = p.pre_tool_call(tool_name="write_file", args={"path": str(home / "memories" / "MEMORY.md")})
            self.assertEqual(result["action"], "block")
            self.assertIn("oracle_only", result["message"])

    def test_blocks_patch_on_user_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "hermes"
            (home / "memories").mkdir(parents=True)
            p = self._oracle_only_provider()
            p._hermes_home = home
            result = p.pre_tool_call(tool_name="patch", args={"file": str(home / "memories" / "USER.md")})
            self.assertEqual(result["action"], "block")

    def test_allows_unrelated_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "hermes"
            (home / "memories").mkdir(parents=True)
            p = self._oracle_only_provider()
            p._hermes_home = home
            self.assertIsNone(p.pre_tool_call(tool_name="write_file", args={"path": str(Path(td) / "notes.txt")}))


if __name__ == "__main__":
    unittest.main()
