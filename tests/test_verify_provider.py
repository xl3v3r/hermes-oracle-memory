"""tests/verify_provider.py is the documented "Verify" step (#19). It must
clean up after itself, hold on any database, and read credentials only from
the environment or an explicit --env-file. No database: the provider is an
in-memory fake."""

import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401  (stubs agent.* / tools.*)
import tests.verify_provider as vp  # noqa: E402

TESTS = Path(__file__).resolve().parent
ENV = {"OCI_DB_USER": "u", "OCI_DB_PASSWORD": "p", "OCI_DB_DSN": "d"}


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, binds=None):
        sid = (binds or {}).get("sid")
        mine = [k for k, r in self.store.rows.items() if r["session_id"] == sid]
        if sql.lstrip().upper().startswith("DELETE"):
            if self.store.fail_cleanup:
                raise RuntimeError("ORA-03113: end-of-file on communication channel")
            self.store.deleted_sessions.append(sid)
            for k in mine:
                del self.store.rows[k]
        else:
            self._result = (len(mine),)

    def fetchone(self):
        return self._result


class FakePool:
    def __init__(self, store):
        self.store = store

    def acquire(self):
        pool = self

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self):
                return FakeCursor(pool.store)

            def commit(self):
                pass

        return Conn()


class FakeProvider:
    """The memory tools over a dict. Knobs simulate a broken install."""

    text_error = ""          # text search raises this
    text_visible = True      # the text index has caught up
    search_error = ""        # every search raises this
    embed_error = ""
    start = True
    instances: list = []

    def __init__(self):
        self.rows = dict(getattr(type(self), "seed", {}))
        self.deleted_sessions = []
        self.fail_cleanup = False
        self._pool = None
        self._table = "T"
        self._onnx_model = "MODEL"
        self._init_error = ""
        self._session_id = ""
        self.shut = False
        type(self).instances.append(self)

    def initialize(self, session_id, **kw):
        self._session_id = session_id
        if type(self).start:
            self._pool = FakePool(self)
        else:
            self._init_error = "ORA-12541: no listener"

    def unavailable_reason(self):
        return ""

    def health(self):
        return {"schema_warnings": list(getattr(type(self), "schema_warnings", []))}

    def _embed(self, text):
        if type(self).embed_error:
            raise RuntimeError(type(self).embed_error)
        return [0.0] * 768

    def prefetch(self, query, **kw):
        return ""

    def shutdown(self):
        self.shut = True

    def handle_tool_call(self, tool, args):
        if tool == "oracle_add":
            facts = args.get("facts") or [args["content"]]
            ids = []
            for i, f in enumerate(facts):
                mid = args.get("memory_id") or f"fact_{len(self.rows)}_{i}"
                self.rows[mid] = {"memory_id": mid, "session_id": self._session_id, "content": f,
                                  "tags": list(args.get("tags") or [])}
                ids.append(mid)
            if len(ids) == 1:
                return json.dumps({"result": "Fact stored.", "memory_id": ids[0]})
            return json.dumps({"result": "Batch", "memory_ids": ids, "count": len(ids)})
        if tool == "oracle_get":
            if args.get("memory_id"):
                rows = [r for k, r in self.rows.items() if k == args["memory_id"]]
            else:
                rows = [r for r in self.rows.values() if set(args["tags"]) <= set(r["tags"])]
            return json.dumps({"results": rows, "count": len(rows)})
        if tool == "oracle_search":
            mode = args["mode"]
            # Hermes' tool_error returns a JSON object with an "error" key.
            if type(self).search_error:
                return json.dumps({"error": type(self).search_error})
            if mode == "text":
                if type(self).text_error:
                    return json.dumps({"error": type(self).text_error})
                if not type(self).text_visible:
                    return json.dumps({"result": "No relevant memories found.", "count": 0})
            words = set(re.findall(r"\w+", args["query"].lower()))
            rows = [r for r in self.rows.values()
                    if (not args.get("session_id") or r["session_id"] == args["session_id"])
                    and words & set(re.findall(r"\w+", r["content"].lower()))]
            return json.dumps({"results": rows, "count": len(rows)})
        if tool == "oracle_update":
            self.rows[args["memory_id"]]["content"] = args["content"]
            return json.dumps({"result": "updated", "memory_id": args["memory_id"]})
        if tool == "oracle_delete":
            self.rows.pop(args["memory_id"])
            return json.dumps({"result": "deleted", "memory_id": args["memory_id"]})
        raise AssertionError(tool)


def _fake_plugin(**knobs):
    cls = type("Provider", (FakeProvider,), dict(knobs, instances=[]))

    class Plugin:
        __version__ = "0.0.0"
        OracleMemoryProvider = cls
        get_secret = staticmethod(lambda k, default=None: "from-host-secret-store")

    return Plugin


def _run(argv=(), env=ENV, **knobs):
    plugin = _fake_plugin(**knobs)
    out = io.StringIO()
    with patch.object(vp, "_load_plugin", return_value=plugin), patch.dict(os.environ, env, clear=True):
        rc = vp.main(["--text-wait", "0", *argv], out=out)
    inst = plugin.OracleMemoryProvider.instances
    return rc, out.getvalue(), (inst[0] if inst else None), plugin


class TestHoldsOnAnyDatabase(unittest.TestCase):
    def test_passes_on_an_empty_database_and_leaves_it_empty(self):
        rc, text, p, _ = _run()
        self.assertEqual(rc, 0, text)
        self.assertIn("0 failed", text)
        self.assertEqual(p.rows, {})

    def test_rows_that_were_there_before_are_neither_judged_nor_touched(self):
        seed = {
            f"old{i}": {"memory_id": f"old{i}", "session_id": "someone-else",
                        "content": "Verification memory provider stores and recalls this sentence", "tags": []}
            for i in range(20)
        }
        rc, text, p, _ = _run(seed=seed)
        self.assertEqual(rc, 0, text)
        self.assertEqual(p.rows, seed)

    def test_session_id_is_unique_per_run(self):
        a = _run()[2]._session_id
        b = _run()[2]._session_id
        self.assertNotEqual(a, b)
        for sid in (a, b):
            self.assertRegex(sid, r"^verify-[0-9a-f]{16}$")

    def test_deletes_only_its_own_session(self):
        _, _, p, _ = _run()
        self.assertEqual(p.deleted_sessions, [p._session_id])


class TestCleanup(unittest.TestCase):
    def test_rows_are_deleted_when_a_check_fails(self):
        rc, text, p, _ = _run(search_error="ORA-00600: internal error")
        self.assertEqual(rc, 1, text)
        self.assertIn("[FAIL] vector search finds the memory: ORA-00600", text)
        self.assertEqual(p.rows, {})
        self.assertEqual(p.deleted_sessions, [p._session_id])

    def test_rows_are_deleted_when_a_check_raises(self):
        plugin = _fake_plugin()
        out = io.StringIO()
        with patch.object(vp, "_load_plugin", return_value=plugin), patch.dict(os.environ, ENV, clear=True), \
                patch.object(plugin.OracleMemoryProvider, "prefetch", side_effect=RuntimeError("boom")):
            rc = vp.main(["--text-wait", "0"], out=out)
        p = plugin.OracleMemoryProvider.instances[0]
        self.assertEqual(rc, 1, out.getvalue())
        self.assertIn("unexpected error", out.getvalue())
        self.assertEqual(p.rows, {})

    def test_failed_cleanup_is_reported_with_the_manual_command(self):
        plugin = _fake_plugin()
        out = io.StringIO()
        real_init = plugin.OracleMemoryProvider.initialize

        def init(self, sid, **kw):
            real_init(self, sid, **kw)
            self.fail_cleanup = True

        with patch.object(vp, "_load_plugin", return_value=plugin), patch.dict(os.environ, ENV, clear=True), \
                patch.object(plugin.OracleMemoryProvider, "initialize", init):
            rc = vp.main(["--text-wait", "0"], out=out)
        p = plugin.OracleMemoryProvider.instances[0]
        self.assertEqual(rc, 1)
        self.assertIn(f'oracle_delete(session_id="{p._session_id}")', out.getvalue())

    def test_provider_is_shut_down_and_host_state_restored(self):
        rc, _, p, plugin = _run(["--table", "SCRATCH"])
        self.assertEqual(rc, 0)
        self.assertTrue(p.shut)
        self.assertEqual(plugin.get_secret("X"), "from-host-secret-store")

    def test_table_option_applies_to_this_run_only(self):
        seen = {}
        plugin = _fake_plugin()
        real_init = plugin.OracleMemoryProvider.initialize

        def init(self, sid, **kw):
            seen["table"] = os.environ.get("HERMES_ORACLE_TABLE")
            real_init(self, sid, **kw)

        with patch.object(vp, "_load_plugin", return_value=plugin), \
                patch.dict(os.environ, dict(ENV, HERMES_ORACLE_TABLE="REAL"), clear=True), \
                patch.object(plugin.OracleMemoryProvider, "initialize", init):
            vp.main(["--text-wait", "0", "--table", "SCRATCH"], out=io.StringIO())
            self.assertEqual(os.environ["HERMES_ORACLE_TABLE"], "REAL")
        self.assertEqual(seen["table"], "SCRATCH")


class TestOutcomes(unittest.TestCase):
    def test_tool_error_that_is_not_json_is_still_an_error(self):
        run = vp.Run(provider=None, session_id="s", text_wait=0)
        run.p = type("P", (), {"handle_tool_call": lambda self, tool, args: "error: plain text"})()
        self.assertEqual(run.call("oracle_search", query="q"), {"error": "error: plain text"})
        self.assertEqual(run.search_ids("q", "text"), ([], "error: plain text"))

    def test_text_search_error_is_a_failure(self):
        rc, text, _, _ = _run(text_error="ORA-30600: Oracle Text error")
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] text search finds the memory: ORA-30600", text)

    def test_row_not_yet_in_text_search_is_a_warning(self):
        rc, text, _, _ = _run(text_visible=False)
        self.assertEqual(rc, 0, text)
        self.assertIn("[warn] text search finds the memory", text)

    def test_strict_turns_that_warning_into_a_failure(self):
        rc, text, _, _ = _run(["--strict"], text_visible=False)
        self.assertEqual(rc, 1, text)

    def test_schema_warnings_are_notes_not_failures(self):
        rc, text, _, _ = _run(["--strict"], schema_warnings=["no vector index on T.EMBEDDING"])
        self.assertEqual(rc, 0, text)
        self.assertIn("[note] schema: no vector index", text)

    def test_missing_model_is_named(self):
        rc, text, p, _ = _run(embed_error="ORA-40284: model does not exist")
        self.assertEqual(rc, 1)
        self.assertIn("ORA-40284", text)
        self.assertIn("Is the ONNX model MODEL loaded", text)
        self.assertEqual(p.rows, {})

    def test_provider_that_does_not_start(self):
        rc, text, _, _ = _run(start=False)
        self.assertEqual(rc, 1)
        self.assertIn("provider did not start: ORA-12541", text)


class TestCredentials(unittest.TestCase):
    def test_missing_settings_exit_2_before_the_plugin_loads(self):
        out = io.StringIO()
        with patch.object(vp, "_load_plugin", side_effect=AssertionError("must not load")), \
                patch.dict(os.environ, {}, clear=True):
            self.assertEqual(vp.main([], out=out), 2)
        self.assertIn("OCI_DB_USER, OCI_DB_PASSWORD, OCI_DB_DSN", out.getvalue())

    def test_env_file_is_read_only_when_passed(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / ".hermes").mkdir()
            (home / ".hermes" / ".env").write_text("OCI_DB_USER=u\nOCI_DB_PASSWORD=p\nOCI_DB_DSN=d\n")
            with patch("pathlib.Path.home", return_value=home), patch.dict(os.environ, {"HOME": td}, clear=True), \
                    patch.object(vp, "_load_plugin", side_effect=AssertionError("must not load")):
                self.assertEqual(vp.main([], out=io.StringIO()), 2)
            rc, _, p, _ = _run(["--env-file", str(home / ".hermes" / ".env")], env={})
            self.assertEqual(rc, 0)

    def test_environment_wins_over_the_env_file(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "e"
            f.write_text("export OCI_DB_USER='file'\nHOM_VP_ONLY_IN_FILE=\"x\"\n# c\n")
            with patch.dict(os.environ, {"OCI_DB_USER": "env"}, clear=True):
                vp.load_env(str(f))
                self.assertEqual(os.environ["OCI_DB_USER"], "env")
                self.assertEqual(os.environ["HOM_VP_ONLY_IN_FILE"], "x")

    def test_unreadable_env_file_exits_2(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(vp.main(["--env-file", "/nonexistent/x.env"], out=io.StringIO()), 2)

    def test_settings_come_from_the_environment_not_the_host_secret_store(self):
        seen = {}
        plugin = _fake_plugin()
        real_init = plugin.OracleMemoryProvider.initialize

        def init(self, sid, **kw):
            seen["user"] = plugin.get_secret("OCI_DB_USER")
            real_init(self, sid, **kw)

        with patch.object(vp, "_load_plugin", return_value=plugin), patch.dict(os.environ, ENV, clear=True), \
                patch.object(plugin.OracleMemoryProvider, "initialize", init):
            vp.main(["--text-wait", "0"], out=io.StringIO())
        self.assertEqual(seen["user"], "u")


class TestSources(unittest.TestCase):
    def test_no_home_env_file_and_no_site_specific_data(self):
        for name in ("verify_provider.py", "test_provider_live.py"):
            src = (TESTS / name).read_text(encoding="utf-8")
            for needle in ("Path.home()", "qwen", "HANDOFF", "pro-se", "test-verification-session"):
                self.assertNotIn(needle, src, f"{name}: {needle}")

    def test_live_test_is_opt_in(self):
        import tests.test_provider_live as live

        with patch.dict(os.environ, dict(ENV), clear=True), \
                patch.object(vp, "main", side_effect=AssertionError("must not run")):
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromModule(live).run(result)
        self.assertEqual(len(result.skipped), result.testsRun)
        self.assertGreaterEqual(result.testsRun, 1)
        self.assertIn("HERMES_ORACLE_LIVE_TESTS", result.skipped[0][1])
        self.assertEqual((result.failures, result.errors), ([], []))

    def test_live_test_runs_the_script_when_opted_in(self):
        import tests.test_provider_live as live

        with patch.dict(os.environ, dict(ENV, HERMES_ORACLE_LIVE_TESTS="1"), clear=True), \
                patch.object(vp, "main", return_value=0) as main:
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromName("TestProviderLive.test_verify_provider_live", live).run(result)
        self.assertEqual((result.failures, result.errors, result.skipped), ([], [], []))
        main.assert_called_once()

    def test_live_test_fails_when_the_script_fails(self):
        import tests.test_provider_live as live

        with patch.dict(os.environ, dict(ENV, HERMES_ORACLE_LIVE_TESTS="1"), clear=True), \
                patch.object(vp, "main", return_value=1):
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromName("TestProviderLive.test_verify_provider_live", live).run(result)
        self.assertEqual(len(result.failures), 1)


if __name__ == "__main__":
    unittest.main()
