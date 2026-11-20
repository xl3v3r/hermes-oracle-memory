import os
import sys
import threading
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

        expected_names = {"oracle_search", "oracle_add", "oracle_update", "oracle_delete", "oracle_chunk", "oracle_sync_skills", "oracle_load_skill", "oracle_get"}
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

                self.assertIn("run `hermes plugins enable oracle` and restart Hermes", provider.unavailable_reason())

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
    def setUp(self):
        # The usage row is written only where the profile asks for it (#28).
        capture = patch.object(oracle_plugin, "_CAPTURE", frozenset({"turn", "session", "skill_usage"}))
        capture.start()
        self.addCleanup(capture.stop)

    def test_register_hooks_on_skill_lifecycle(self):
        mock_ctx = MagicMock()
        oracle_plugin.register(mock_ctx)
        mock_ctx.register_memory_provider.assert_called_once()
        # on_skill_lifecycle must stay the FIRST hook registration: the skill-usage
        # tests pick the first callback (#38 added pre_llm_call and pre_tool_call).
        self.assertEqual(
            [c[0][0] for c in mock_ctx.register_hook.call_args_list],
            ["on_skill_lifecycle", "pre_llm_call", "pre_tool_call"],
        )
        cb = mock_ctx.register_hook.call_args_list[0][0][1]
        self.assertTrue(callable(cb))

    def test_on_skill_lifecycle_used_action(self):
        mock_ctx = MagicMock()
        oracle_plugin.register(mock_ctx)
        provider = mock_ctx.register_memory_provider.call_args[0][0]
        hook_cb = mock_ctx.register_hook.call_args_list[0][0][1]

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
        hook_cb = mock_ctx.register_hook.call_args_list[0][0][1]

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
        hook_cb = mock_ctx.register_hook.call_args_list[0][0][1]

        for ignored in ("created", "archived", "patched", "edited", "unknown"):
            with patch.object(provider, "_queue_background_insert") as mock_insert:
                hook_cb(action=ignored, skill_name="test-skill", session_id="s1")
                mock_insert.assert_not_called()

    def test_on_skill_lifecycle_empty_skill_name(self):
        mock_ctx = MagicMock()
        oracle_plugin.register(mock_ctx)
        provider = mock_ctx.register_memory_provider.call_args[0][0]
        hook_cb = mock_ctx.register_hook.call_args_list[0][0][1]

        with patch.object(provider, "_queue_background_insert") as mock_insert:
            hook_cb(action="used", skill_name="", session_id="s1")
            mock_insert.assert_not_called()

    def test_on_skill_lifecycle_failsafe_on_exception(self):
        mock_ctx = MagicMock()
        oracle_plugin.register(mock_ctx)
        provider = mock_ctx.register_memory_provider.call_args[0][0]
        hook_cb = mock_ctx.register_hook.call_args_list[0][0][1]

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
        hook_cb = mock_ctx.register_hook.call_args_list[0][0][1]

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
        hook_cb = mock_ctx.register_hook.call_args_list[0][0][1]

        with patch.object(provider, "_queue_background_insert") as mock_insert:
            hook_cb(action="used", skill_name="test-skill")
            mock_insert.assert_called_once()
            kwargs = mock_insert.call_args[1]
            self.assertEqual(kwargs["session_id"], "provider-session-default")
            self.assertIn("[session:provider-session-default]", kwargs["content"])

    def test_load_module_from_path_handles_dataclasses(self):
        import tempfile, textwrap
        from pathlib import Path
        src = textwrap.dedent(
            '''
            from dataclasses import dataclass

            @dataclass
            class Thing:
                name: str

            def ping():
                return Thing("ok")
            '''
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "probe_mod.py"
            p.write_text(src, encoding="utf-8")
            mod = oracle_plugin._load_module_from_path("probe_mod", p)
            self.assertEqual(mod.ping().name, "ok")
            self.assertIn("probe_mod", sys.modules)


class TestHookSurvivesProviderShutdown(unittest.TestCase):
    """Hermes re-binds the global on_skill_lifecycle hook to the NEWEST provider
    instance on every load. When that instance shuts down, skill events from
    other live sessions must reach a live provider, not fail with
    'pool not initialized'."""

    def _live(self, sid):
        p = oracle_plugin.OracleMemoryProvider()
        p._pool = MagicMock()
        p._initialized = True
        p._session_id = sid
        p._tuning = dict(oracle_plugin._read_tuning(use_module_values=True), capture=frozenset({"skill_usage"}))
        with oracle_plugin._LIVE_PROVIDERS_LOCK:
            oracle_plugin._LIVE_PROVIDERS.add(p)
        return p

    def tearDown(self):
        with oracle_plugin._LIVE_PROVIDERS_LOCK:
            oracle_plugin._LIVE_PROVIDERS.clear()

    def test_hook_on_dead_provider_routes_to_live_one(self):
        a = self._live("sess-A")
        b = self._live("sess-B")          # hook is bound to b (newest)
        b._write_stop.set(); b._pool = None   # what shutdown() leaves behind
        with oracle_plugin._LIVE_PROVIDERS_LOCK:
            oracle_plugin._LIVE_PROVIDERS.discard(b)

        with patch.object(a, "_queue_background_insert") as a_ins, \
             patch.object(b, "_insert") as b_insert:
            b.on_skill_lifecycle(action="loaded", skill_name="probe", session_id="sess-A")
            a_ins.assert_called_once()
            self.assertEqual(a_ins.call_args[1]["session_id"], "sess-A")
            b_insert.assert_not_called()

    def test_fallback_picks_newest_registered_provider(self):
        provs = []
        for i in range(6):
            p = oracle_plugin.OracleMemoryProvider()
            p._pool = MagicMock(); p._initialized = True; p._session_id = f"s{i}"
            oracle_plugin._register_live_provider(p)
            provs.append(p)
        dead = oracle_plugin.OracleMemoryProvider()
        self.assertIs(dead._live_provider_for("no-such-session"), provs[-1])

    def test_shutdown_does_not_wait_on_or_leak_pool_for_prefetch(self):
        p = oracle_plugin.OracleMemoryProvider()
        pool = MagicMock()
        p._pool = pool; p._initialized = True
        pf = MagicMock(); pf.is_alive.return_value = True
        p._prefetch_thread = pf
        p.shutdown()
        pf.join.assert_called_once_with(timeout=3.0)
        pool.close.assert_called_once()
        self.assertIsNone(p._pool)

    def test_shutdown_defers_close_while_a_connection_is_out(self):
        p = oracle_plugin.OracleMemoryProvider()
        pool = MagicMock()
        pool.close.side_effect = Exception("DPY-1005: unable to close pool with busy connections")
        p._pool = pool; p._initialized = True
        called = threading.Event()
        closer = MagicMock(side_effect=lambda *a, **k: called.set())
        with patch.object(oracle_plugin, "_close_pool_when_idle", closer):
            p.shutdown()
            self.assertTrue(called.wait(5))
        closer.assert_called_once_with(pool)
        pool.close.assert_called_once_with()  # never force-closed inline
        self.assertIsNone(p._pool)

    def test_shutdown_waits_for_skill_lifecycle_writer(self):
        p = oracle_plugin.OracleMemoryProvider()
        pool = MagicMock()
        p._pool = pool; p._initialized = True
        w = MagicMock(); w.is_alive.return_value = True  # DELETE done, INSERT pending
        p._sync_threads = [w]
        p.shutdown()
        # shutdown() joins within its own 30 s budget; the deferred finisher
        # (1.1.1.4) may join again later, but never closes under a live writer.
        self.assertLessEqual(w.join.call_args_list[0].kwargs["timeout"], 30.0)
        pool.close.assert_not_called()  # busy: never close under a lifecycle writer
        self.assertIs(p._pool, pool)

    def test_close_pool_when_idle_waits_then_closes_without_force(self):
        from unittest.mock import call
        pool = MagicMock()
        busy = Exception("DPY-1005: unable to close pool with busy connections")
        pool.close.side_effect = [busy, busy, None]
        oracle_plugin._close_pool_when_idle(pool, budget=5.0, poll=0.0)
        self.assertEqual(pool.close.call_args_list, [call(), call(), call()])

    def test_close_pool_when_idle_forces_after_budget(self):
        from unittest.mock import call
        pool = MagicMock()
        oracle_plugin._close_pool_when_idle(pool, budget=0.0, poll=0.0)
        self.assertEqual(pool.close.call_args_list, [call(force=True)])

    def test_sync_thread_started_under_lock_and_dead_ones_pruned(self):
        p = oracle_plugin.OracleMemoryProvider()
        dead = MagicMock(); dead.is_alive.return_value = False
        p._sync_threads = [dead] * 50
        live = MagicMock(); live.is_alive.return_value = True
        live.start.side_effect = lambda: self.assertTrue(p._sync_threads_lock.locked())
        self.assertTrue(p._start_sync_thread(live))
        live.start.assert_called_once()
        self.assertEqual(p._sync_threads, [live])

    def test_sync_thread_refused_once_shutdown_began(self):
        p = oracle_plugin.OracleMemoryProvider()
        p._write_stop.set()
        t = MagicMock()
        self.assertFalse(p._start_sync_thread(t))
        t.start.assert_not_called()
        self.assertEqual(p._sync_threads, [])

    def test_unstarted_thread_cannot_be_pruned_by_concurrent_tracking(self):
        # Every listed thread was started under the lock, so a concurrent
        # _start_sync_thread() never drops one that is about to run.
        p = oracle_plugin.OracleMemoryProvider()
        gate = threading.Event()
        first = threading.Thread(target=gate.wait, daemon=True)
        second = threading.Thread(target=gate.wait, daemon=True)
        try:
            self.assertTrue(p._start_sync_thread(first))
            self.assertTrue(p._start_sync_thread(second))
            self.assertEqual(p._sync_threads, [first, second])
        finally:
            gate.set(); first.join(2); second.join(2)

    def test_queue_on_dead_provider_is_handed_off_not_failed(self):
        a = self._live("sess-A")
        b = oracle_plugin.OracleMemoryProvider()
        b._initialized = True; b._pool = None; b._write_stop.set()
        with patch.object(a, "_queue_background_insert") as a_ins:
            b._queue_background_insert(memory_id="m1", session_id="sess-A", role="fact",
                                       content="c", target="memory", metadata={})
            a_ins.assert_called_once()
        self.assertTrue(b._write_queue.empty())

    def test_queue_on_dead_provider_with_no_live_peer_drops_cleanly(self):
        b = oracle_plugin.OracleMemoryProvider()
        b._initialized = True; b._pool = None; b._write_stop.set()
        b._queue_background_insert(memory_id="m1", session_id="s", role="fact",
                                   content="c", target="memory", metadata={})
        self.assertTrue(b._write_queue.empty())
        self.assertIsNone(b._write_worker)

    def test_shutdown_unregisters_provider(self):
        a = self._live("sess-A")
        a._write_worker = a._embed_worker = a._prefetch_thread = None
        a.shutdown()
        self.assertNotIn(a, list(oracle_plugin._LIVE_PROVIDERS))


if __name__ == '__main__':
    unittest.main()
