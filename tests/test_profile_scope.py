"""Profile scope (#24): the provider binds its home, its settings and its
background threads to the profile Hermes initialized it for. No database."""

import contextvars
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401  (stubs agent.* / tools.*)
import __init__ as oracle_plugin  # noqa: E402

CREDS = {"OCI_DB_USER": "u", "OCI_DB_PASSWORD": "p", "OCI_DB_DSN": "host:1521/svc"}


def _initialize(settings=None, **kwargs):
    """initialize() with a mocked pool; returns (provider, pool kwargs)."""
    values = dict(CREDS, **(settings or {}))
    p = oracle_plugin.OracleMemoryProvider()
    with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, d=None: values.get(k, d)), \
            patch("oracledb.SessionPool") as pool, \
            patch.object(p, "_ensure_schema"), patch.object(p, "_start_backfill_scan"):
        p.initialize("s1", **kwargs)
    return p, (pool.call_args[1] if pool.call_args else {})


def _wallet(base: Path) -> Path:
    d = base / "oracle_wallet"
    d.mkdir(parents=True)
    (d / "cwallet.sso").write_text("x")
    return d


class _Saved(unittest.TestCase):
    """initialize() refreshes module-level defaults; put them back."""

    def setUp(self):
        names = ("_SEARCH_TIMEOUT_MS", "_SEARCH_TIMEOUT_SECS", "_INGEST_TIMEOUT_MS", "_INGEST_TIMEOUT_SECS")
        saved = {n: getattr(oracle_plugin, n) for n in names}
        self.addCleanup(lambda: [setattr(oracle_plugin, n, v) for n, v in saved.items()])


class TestHermesHome(_Saved):
    def test_wallet_is_found_under_the_home_hermes_passed(self):
        # The lookup used ~/.hermes/oracle_wallet whatever the profile home was.
        with tempfile.TemporaryDirectory() as td:
            profile = Path(td) / "profiles" / "work"
            wallet = _wallet(profile)
            with patch("pathlib.Path.home", return_value=Path(td) / "home"), patch.dict(os.environ, {}, clear=True):
                p, kw = _initialize(hermes_home=str(profile))
            self.assertEqual(p._hermes_home, profile)
            self.assertEqual(kw["config_dir"], str(wallet))
            self.assertEqual(kw["wallet_location"], str(wallet))

    def test_default_home_wallet_is_not_used_for_another_profile(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            _wallet(home / ".hermes")
            profile = Path(td) / "profiles" / "work"
            profile.mkdir(parents=True)
            with patch("pathlib.Path.home", return_value=home), patch.dict(os.environ, {}, clear=True):
                _, kw = _initialize(hermes_home=str(profile))
                self.assertNotIn("wallet_location", kw)
                # Without the kwarg (older Hermes) the default home still works.
                _, kw = _initialize()
                self.assertEqual(kw["wallet_location"], str(home / ".hermes" / "oracle_wallet"))

    def test_home_order_without_the_kwarg(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(sys.modules, {"hermes_constants": None}), patch("pathlib.Path.home", return_value=Path(td)):
                with patch.dict(os.environ, {"HERMES_HOME": str(Path(td) / "env-home")}):
                    self.assertEqual(oracle_plugin._hermes_home_path(), Path(td) / "env-home")
                with patch.dict(os.environ, {"HERMES_HOME": ""}):
                    self.assertEqual(oracle_plugin._hermes_home_path(), Path(td) / ".hermes")
            host = MagicMock()
            host.get_hermes_home.return_value = str(Path(td) / "profile-home")
            with patch.dict(sys.modules, {"hermes_constants": host}):
                self.assertEqual(oracle_plugin._hermes_home_path(), Path(td) / "profile-home")
                self.assertEqual(oracle_plugin._hermes_home_path("/explicit"), Path("/explicit"))

    def test_explicit_wallet_settings_come_from_the_profile(self):
        with patch.dict(os.environ, {"TNS_ADMIN": "/process/tns"}, clear=True):
            with tempfile.TemporaryDirectory() as td:
                Path(td, "w").mkdir()
                _, kw = _initialize({"OCI_WALLET_LOCATION": str(Path(td, "w")), "OCI_WALLET_PASSWORD": "wp"})
                self.assertEqual((kw["wallet_location"], kw["wallet_password"]), (str(Path(td, "w")), "wp"))
                Path(td, "t").mkdir()
                _, kw = _initialize({"TNS_ADMIN": str(Path(td, "t"))})
                self.assertEqual(kw["config_dir"], str(Path(td, "t")))

    def test_trusted_roots_use_the_profile_home(self):
        host = MagicMock()
        host.get_hermes_home.return_value = "/var/lib/hermes/profiles/work"
        with patch.dict(sys.modules, {"hermes_constants": host}), patch.dict(os.environ, {"HERMES_HOME": ""}):
            r = oracle_plugin._sensitive_path_reason
            # The function takes resolved paths (on macOS /var is /private/var).
            self.assertEqual(r(Path("/var/lib/hermes/profiles/work/skills/x/SKILL.md").resolve()), "")
            self.assertEqual(r(Path("/var/lib/hermes/profiles/other/notes.md").resolve()), "system path")


class TestProfileSettings(_Saved):
    def test_settings_are_read_from_the_profile_scope_first(self):
        with patch.dict(os.environ, {"HOM_X": "from-env"}):
            with patch.object(oracle_plugin, "get_secret", return_value="from-profile"):
                self.assertEqual(oracle_plugin._setting("HOM_X"), "from-profile")
            with patch.object(oracle_plugin, "get_secret", return_value=None):
                self.assertEqual(oracle_plugin._setting("HOM_X"), "from-env")
            with patch.object(oracle_plugin, "get_secret", return_value="   "):
                self.assertEqual(oracle_plugin._setting("HOM_X"), "from-env")
        with patch.object(oracle_plugin, "get_secret", return_value=None), patch.dict(os.environ):
            os.environ.pop("HOM_X", None)
            self.assertEqual(oracle_plugin._setting("HOM_X", "dflt"), "dflt")

    def test_a_secret_read_that_fails_closed_falls_back_to_the_environment(self):
        # On a multiplexed gateway get_secret raises when no profile scope is
        # bound (import time, a thread without context).
        with patch.object(oracle_plugin, "get_secret", side_effect=RuntimeError("no profile scope")), \
                patch.dict(os.environ, {"HOM_X": "deployment"}):
            self.assertEqual(oracle_plugin._setting("HOM_X"), "deployment")

    def test_each_provider_keeps_its_own_timeouts(self):
        # initialize() rewrote module-level values shared by every provider:
        # the profile initialized last decided for all.
        with patch.dict(os.environ, {}, clear=True):
            a, kw_a = _initialize({"HERMES_ORACLE_SEARCH_TIMEOUT_MS": "5000", "HERMES_ORACLE_INGEST_TIMEOUT": "60"})
            b, kw_b = _initialize({"HERMES_ORACLE_SEARCH_TIMEOUT_MS": "45000"})
        self.assertEqual((a._search_ms, a._search_secs, a._ingest_ms), (5000, 5.0, 60000))
        self.assertEqual((b._search_ms, b._ingest_ms), (45000, 600000))
        self.assertEqual((kw_a["call_timeout"], kw_b["call_timeout"]), (60000, 600000))

    def test_uninitialized_provider_uses_the_process_defaults(self):
        p = oracle_plugin.OracleMemoryProvider()
        with patch.object(oracle_plugin, "_SEARCH_TIMEOUT_MS", 1234), patch.object(oracle_plugin, "_INGEST_TIMEOUT_MS", 99):
            self.assertEqual((p._search_ms, p._ingest_ms), (1234, 99))
        with patch.object(oracle_plugin, "_MAX_CHUNKS_PER_SOURCE", 7):
            self.assertEqual(p._tune("max_chunks_per_source"), 7)

    def test_each_provider_keeps_its_own_ranking_settings(self):
        with patch.dict(os.environ, {}, clear=True):
            a, _ = _initialize({"HERMES_ORACLE_MAX_CHUNKS_PER_SKILL": "5", "HERMES_ORACLE_DISABLE_RERANK": "1",
                                "HERMES_ORACLE_MIN_CANDIDATE_POOL": "80", "HERMES_ORACLE_RECENCY_FLOOR": "0.5"})
            b, _ = _initialize()
        self.assertEqual((a._tune("max_chunks_per_source"), a._tune("rerank_enabled"),
                          a._tune("min_candidate_pool"), a._tune("recency_floor")), (5, False, 80, 0.5))
        self.assertEqual((b._tune("max_chunks_per_source"), b._tune("rerank_enabled"),
                          b._tune("min_candidate_pool"), b._tune("recency_floor")), (2, True, 50, 0.85))
        rows = [{"memory_id": f"m{i}", "conv_ref": "one-source", "score": 0.9} for i in range(6)]
        self.assertEqual(len(a._diversify_rows(list(rows))), 5)
        self.assertEqual(len(b._diversify_rows(list(rows))), 2)

    def test_bad_ranking_values_fall_back_instead_of_breaking_the_plugin(self):
        # float(os.environ[...]) at import made a typo stop the plugin from loading.
        with patch.dict(os.environ, {}, clear=True), self.assertLogs(oracle_plugin.logger.name, level="WARNING"):
            p, _ = _initialize({"HERMES_ORACLE_RECENCY_FLOOR": "high", "HERMES_ORACLE_MAX_CHUNKS_PER_SKILL": "-3",
                                "HERMES_ORACLE_TELEMETRY_WEIGHT": "nan"})
        self.assertEqual((p._tune("recency_floor"), p._tune("max_chunks_per_source"), p._tune("telemetry_weight")),
                         (0.85, 2, 0.05))

    def test_zero_is_a_valid_cap_and_weight(self):
        with patch.dict(os.environ, {}, clear=True):
            p, _ = _initialize({"HERMES_ORACLE_MAX_CHUNKS_PER_SKILL": "0", "HERMES_ORACLE_TELEMETRY_WEIGHT": "0"})
        self.assertEqual((p._tune("max_chunks_per_source"), p._tune("telemetry_weight")), (0, 0.0))
        rows = [{"memory_id": f"m{i}", "conv_ref": "s", "score": 0.9} for i in range(6)]
        self.assertEqual(len(p._diversify_rows(rows)), 6)  # 0 = no cap

    def test_schema_policy_is_the_profiles(self):
        with patch.dict(os.environ, {"HERMES_ORACLE_SCHEMA_POLICY": ""}):
            p, _ = _initialize({"HERMES_ORACLE_SCHEMA_POLICY": "require_existing"})
        self.assertEqual(p._schema_policy, "REQUIRE_EXISTING")
        p._pool = MagicMock()
        with patch.object(p, "_verify_schema") as verify:
            p._ensure_schema()
        verify.assert_called_once_with()
        p._pool.acquire.assert_not_called()  # no DDL


class TestContextThreads(unittest.TestCase):
    def test_background_threads_use_hermes_spawn_context_thread(self):
        var = contextvars.ContextVar("hom_profile", default="none")
        seen = []

        def spawn(target, *, name, daemon=True, args=(), kwargs=None):
            ctx = contextvars.copy_context()
            return threading.Thread(target=lambda: ctx.run(target, *args, **(kwargs or {})), name=name, daemon=daemon)

        with patch.object(oracle_plugin, "_spawn_context_thread", spawn):
            var.set("profile-a")
            t = oracle_plugin._spawn_thread(lambda tag: seen.append((tag, var.get())), name="x", args=("job",))
            var.set("changed-after-spawn")
            t.start()
            t.join(5)
        self.assertEqual(seen, [("job", "profile-a")])
        self.assertTrue(t.daemon)

    def test_older_hermes_falls_back_to_a_plain_thread(self):
        with patch.object(oracle_plugin, "_spawn_context_thread", None):
            done = threading.Event()
            t = oracle_plugin._spawn_thread(done.set, name="plain")
            self.assertIsInstance(t, threading.Thread)
            self.assertTrue(t.daemon)
            self.assertEqual(t.name, "plain")
            t.start()
            self.assertTrue(done.wait(5))

    def test_the_provider_creates_no_plain_threads(self):
        import ast

        tree = ast.parse(Path(oracle_plugin.__file__).read_text(encoding="utf-8"))
        plain = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Thread":
                plain.append(node.lineno)
        # The one allowed call is the fallback inside _spawn_thread.
        self.assertEqual(len(plain), 1, plain)

    def test_prefetch_search_runs_in_the_callers_context(self):
        var = contextvars.ContextVar("hom_profile2", default="none")
        seen = []

        def spawn(target, *, name, daemon=True, args=(), kwargs=None):
            ctx = contextvars.copy_context()
            return threading.Thread(target=lambda: ctx.run(target, *args, **(kwargs or {})), name=name, daemon=daemon)

        p = oracle_plugin.OracleMemoryProvider()
        p._pool = MagicMock()
        with patch.object(oracle_plugin, "_spawn_context_thread", spawn), \
                patch.object(p, "_search", side_effect=lambda *a, **k: seen.append(var.get()) or []):
            var.set("profile-b")
            p.prefetch("what is the relay port")
        self.assertEqual(seen, ["profile-b"])

    def test_session_callback_does_not_keep_the_provider_alive(self):
        # The pool holds the callback; a callback that holds the provider is a
        # cycle, and a dropped provider stayed in the live registry.
        import gc
        import weakref

        gc.disable()
        try:
            values = dict(CREDS)
            p = oracle_plugin.OracleMemoryProvider()
            with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, d=None: values.get(k, d)), \
                    patch("oracledb.SessionPool") as pool, \
                    patch.object(p, "_ensure_schema"), patch.object(p, "_start_backfill_scan"):
                p.initialize("s1")
                callback = pool.call_args[1]["session_callback"]
            ref = weakref.ref(p)
            del p
            self.assertIsNone(ref(), "the provider is still referenced")
            self.assertTrue(callable(callback))
        finally:
            gc.enable()


if __name__ == "__main__":
    unittest.main()
