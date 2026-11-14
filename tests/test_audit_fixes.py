"""1.1.1.3 audit fixes: secret-path guard, delete guard, sync_skills argument
validation, in-process sync module loading, .archive handling, timeout drop,
prefetch start race, failed-init cleanup. Fully mocked; touches no real HOME."""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401  (stubs agent.* / tools.*)
import __init__ as oracle_plugin  # noqa: E402


def _provider(rowcount=1):
    p = oracle_plugin.OracleMemoryProvider()
    p._table = "HERMES_AGENT_MEMORY"
    p._onnx_model = "BGE_BASE_EN_V1_5"
    conn, cur = MagicMock(), MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    cur.rowcount = rowcount
    p._pool = MagicMock()
    p._pool.acquire.return_value = conn
    return p, cur


class TestChunkSecretGuard(unittest.TestCase):
    def test_refuses_secret_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".ssh").mkdir()
            paths = [root / ".env", root / ".env.local", root / "auth.json", root / "id_ed25519",
                     root / "server.pem", root / "cwallet.sso", root / ".ssh" / "config"]
            for f in paths:
                f.write_text("SECRET=1\n")
            for f in paths:
                p, cur = _provider()
                out = p.handle_tool_call("oracle_chunk", {"file_path": str(f)})
                self.assertIn("Refusing to read", out, f)
                cur.execute.assert_not_called()

    def test_refuses_traversal_into_secret_dir(self):
        # The path is resolve()d before the check, so ../ (and symlinks) cannot hide it.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".aws").mkdir()
            (root / "docs").mkdir()
            (root / ".aws" / "credentials").write_text("k=v\n")
            p, _ = _provider()
            sneaky = str(root / "docs" / ".." / ".aws" / "credentials")
            self.assertIn("Refusing to read", p.handle_tool_call("oracle_chunk", {"file_path": sneaky}))

    def test_refuses_system_paths(self):
        for f in ("/etc/hostname", "/proc/version", "/sys/kernel"):
            p, _ = _provider()
            self.assertIn("Refusing to read", p.handle_tool_call("oracle_chunk", {"file_path": f}))

    def test_macos_private_roots_refused(self):
        for f in ("/private/etc/ssh/sshd_config", "/private/var/db/x", "/private/var/root/notes"):
            self.assertEqual(oracle_plugin._sensitive_path_reason(Path(f)), "system path", f)

    def test_home_and_hermes_home_exempt_from_system_roots_only(self):
        hh = Path("/var/lib/hermes").resolve()  # /private/var/lib/hermes on macOS
        with patch.object(oracle_plugin.Path, "home", return_value=Path("/root")), \
                patch.dict(os.environ, {"HERMES_HOME": str(hh)}):
            r = oracle_plugin._sensitive_path_reason
            self.assertEqual(r(Path("/root/docs/notes.md")), "")
            self.assertEqual(r(hh / "skills" / "x" / "SKILL.md"), "")
            self.assertNotEqual(r(Path("/root") / ".ssh" / "config"), "")
            self.assertNotEqual(r(hh / ".env"), "")
            self.assertEqual(r(Path("/var/lib/postgresql/data").resolve()), "system path")
            self.assertEqual(r(Path("/etc/hostname")), "system path")

    def test_misconfigured_home_does_not_exempt_system_roots(self):
        r = oracle_plugin._sensitive_path_reason
        for home, probe in (("/etc", "/etc/ssh/ssh_host_ed25519_key"), ("/var", "/var/lib/x"),
                            ("/var/lib", "/var/lib/x")):
            with patch.object(oracle_plugin.Path, "home", return_value=Path(home)), \
                    patch.dict(os.environ, {"HERMES_HOME": home}):
                # resolve() the probe like real callers (macOS: /etc -> /private/etc)
                self.assertEqual(r(Path(probe).resolve()), "system path", home)

    def test_home_lookup_failure_fails_closed(self):
        with patch.object(oracle_plugin.Path, "home", side_effect=RuntimeError("no home")), \
                patch.dict(os.environ, {"HERMES_HOME": ""}):
            self.assertEqual(oracle_plugin._sensitive_path_reason(Path("/root/x")), "system path")

    def test_tool_token_stores_refused(self):
        r = oracle_plugin._sensitive_path_reason
        for f in ("/home/u/.config/gcloud/application_default_credentials.json",
                  "/home/u/.config/gh/hosts.yml", "/home/u/.cargo/credentials.toml",
                  "/home/u/proj/.envrc", "/home/u/.bash_history"):
            self.assertNotEqual(r(Path(f)), "", f)
        self.assertEqual(r(Path("/home/u/.config/myapp/settings.json")), "")

    def test_plain_document_is_allowed(self):
        for name in ("notes.md", "export.json", "env-setup.txt"):
            self.assertEqual(oracle_plugin._sensitive_path_reason(Path("/home/u/docs") / name), "", name)


class TestInsertManyInCallerTransaction(unittest.TestCase):
    ROWS = [{"memory_id": "skill:h:x:c0", "content": "body", "role": "skill"}]

    def test_uses_caller_connection_and_never_commits(self):
        p, _ = _provider()
        p._pool = None  # shutdown() may have cleared it; the caller's connection is enough
        conn, cur = MagicMock(), MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        self.assertEqual(p._insert_many(self.ROWS, tx_conn=conn), [])
        cur.executemany.assert_called_once()
        conn.commit.assert_not_called()
        self.assertEqual(cur.execute.call_args_list[0].args[0], "SAVEPOINT hermes_insert_many")

    def test_failed_attempts_roll_back_to_savepoint_and_backfill_is_deferred(self):
        p, _ = _provider()
        conn, cur = MagicMock(), MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        shape = Exception("ORA-54401: embedding input too long")
        cur.executemany.side_effect = [shape, shape, None]  # full -> conservative -> NULL embedding
        with patch.object(p, "_queue_embed_backfill") as q:
            out = p._insert_many(self.ROWS, tx_conn=conn)
        self.assertEqual(out, [("skill:h:x:c0", "body")])
        q.assert_not_called()  # caller queues after its commit
        conn.commit.assert_not_called()
        stmts = [c.args[0] for c in cur.execute.call_args_list]
        self.assertEqual(stmts.count("ROLLBACK TO SAVEPOINT hermes_insert_many"), 2)
        self.assertFalse(p._onnx_in_cooldown())


class TestDeleteGuard(unittest.TestCase):
    def test_reserved_session_ids_refused(self):
        for sid in ("skills", "none", "NONE", " skills "):
            p, cur = _provider()
            out = p.handle_tool_call("oracle_delete", {"session_id": sid})
            self.assertIn("cannot be purged", out, sid)
            cur.execute.assert_not_called()

    def test_both_ids_refused(self):
        p, cur = _provider()
        out = p.handle_tool_call("oracle_delete", {"session_id": "s1", "memory_id": "fact_1"})
        self.assertIn("not both", out)
        cur.execute.assert_not_called()

    def test_session_purge_spares_skill_rows(self):
        p, cur = _provider(rowcount=3)
        out = json.loads(p.handle_tool_call("oracle_delete", {"session_id": "thread-1"}))
        self.assertEqual(out["deleted_count"], 3)
        sql, binds = cur.execute.call_args[0]
        self.assertIn("role <> 'skill'", sql)
        self.assertEqual(binds, ("thread-1",))


class TestSyncSkillsArgs(unittest.TestCase):
    def _call(self, args):
        p, _ = _provider()
        p.sync_skills = MagicMock(return_value={"status": "success"})
        return p, p.handle_tool_call("oracle_sync_skills", args)

    def test_string_dirs_is_one_dir_not_characters(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "skills"
            d.mkdir()
            p, _ = self._call({"dirs": str(d)})
            self.assertEqual(p.sync_skills.call_args[1]["dirs"], [d.resolve()])

    def test_root_home_and_secret_dirs_refused(self):
        for bad in ("/", "/home", str(Path.home()), str(Path.home() / ".ssh"), "/etc"):
            p, out = self._call({"dirs": [bad]})
            self.assertIn("refusing to scan", out, bad)
            p.sync_skills.assert_not_called()

    def test_deep_home_ancestors_refused(self):
        with patch.object(oracle_plugin.Path, "home", return_value=Path("/srv/users/u")):
            for bad in ("/srv/users", "/srv/users/u"):
                p, out = self._call({"dirs": [bad]})
                self.assertIn("refusing to scan", out, bad)
                p.sync_skills.assert_not_called()

    def test_force_text_false_is_false(self):
        for raw, want in (("false", False), ("0", False), ("true", True), (True, True), (None, False)):
            p, _ = self._call({"force": raw})
            self.assertIs(p.sync_skills.call_args[1]["force"], want, raw)

    def test_bad_mode_refused(self):
        p, out = self._call({"mode": "everything"})
        self.assertIn("mode must be", out)
        p.sync_skills.assert_not_called()


class TestSyncModuleLoading(unittest.TestCase):
    def test_loads_by_path_despite_foreign_scripts_package(self):
        saved = {k: sys.modules.pop(k) for k in ("scripts", oracle_plugin._SYNC_MOD_NAME) if k in sys.modules}
        sys.modules["scripts"] = types.ModuleType("scripts")  # the host's own `scripts`
        env_before, path_before = dict(os.environ), list(sys.path)
        try:
            mod = oracle_plugin._sync_skills_module()
            self.assertTrue(mod.__file__.endswith(os.path.join("scripts", "sync_skills.py")))
            self.assertTrue(callable(mod.parse_skill_file))
            self.assertIs(oracle_plugin._sync_skills_module(), mod)  # cached
            self.assertEqual(dict(os.environ), env_before)  # no .env side effects in-process
            self.assertEqual(sys.path, path_before)
        finally:
            sys.modules.pop("scripts", None)
            sys.modules.pop(oracle_plugin._SYNC_MOD_NAME, None)
            sys.modules.update(saved)

    def test_ingest_timeout_parsing_is_clamped(self):
        mod = oracle_plugin._sync_skills_module()
        for raw, want in (("", 600000), ("abc", 600000), ("0", 600000), ("10", 30000),
                          ("600.5", 600500), ("99999", 7200000)):
            with patch.dict(os.environ, {"HERMES_ORACLE_INGEST_TIMEOUT": raw}):
                self.assertEqual(mod._ingest_timeout_ms(), want, raw)


class TestArchivedSkillLookup(unittest.TestCase):
    def test_archive_copy_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".archive" / "foo").mkdir(parents=True)
            (root / ".archive" / "foo" / "SKILL.md").write_text("x")
            self.assertEqual(oracle_plugin._skill_md_matches(root, "foo"), [])
            (root / "cat" / "foo").mkdir(parents=True)
            (root / "cat" / "foo" / "SKILL.md").write_text("x")
            self.assertEqual(oracle_plugin._skill_md_matches(root, "foo"), [root / "cat" / "foo" / "SKILL.md"])


class TestPrefetchStartRace(unittest.TestCase):
    def test_starts_the_thread_it_created(self):
        p, _ = _provider()
        replacement = MagicMock()

        class _RacyLock:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                p._prefetch_thread = replacement  # a concurrent call wins the attribute
                return False

        p._lock = _RacyLock()
        created = MagicMock()
        with patch.object(oracle_plugin.threading, "Thread", return_value=created):
            p._start_prefetch("what did we decide about the billing migration")
        created.start.assert_called_once()
        replacement.start.assert_not_called()


class TestFailedInitCleanup(unittest.TestCase):
    def test_schema_failure_closes_new_pool_and_unregisters(self):
        secrets = {"OCI_DB_USER": "U", "OCI_DB_PASSWORD": "PW", "OCI_DB_DSN": "db_low"}
        with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, d=None: secrets.get(k, d)):
            with patch("oracledb.SessionPool") as sp:
                raw_pool = sp.return_value
                p = oracle_plugin.OracleMemoryProvider()
                p._ensure_schema = MagicMock(side_effect=Exception("ORA-01031: insufficient privileges"))
                p.initialize("s1")
        self.assertIsNone(p._pool)
        self.assertFalse(p._initialized)
        self.assertNotIn(p, oracle_plugin._LIVE_PROVIDERS)
        raw_pool.close.assert_called_once_with(force=True)
        self.assertIn("ORA-01031", p._init_error)


if __name__ == "__main__":
    unittest.main()
