"""Skill corpus identity and lifecycle (#5, #6). Mocked; no DB."""
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import tests.conftest  # noqa: F401
import __init__ as oracle_plugin
from scripts import sync_skills as ss

REPO = Path(root_dir)


def _provider():
    p = oracle_plugin.OracleMemoryProvider()
    p._pool = MagicMock()
    conn = MagicMock()
    cur = MagicMock()
    p._pool.acquire.return_value = conn
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    return p, conn, cur


class TestHostId(unittest.TestCase):
    def test_explicit_id_wins(self):
        with patch.dict(os.environ, {"HERMES_HOST_ID": "Box-1", "HOSTNAME": "other"}):
            self.assertEqual(oracle_plugin.get_host_id(), "Box-1")
            self.assertEqual(ss.get_host_id(), "Box-1")

    def test_short_lower_hostname_same_with_or_without_env(self):
        env = {k: v for k, v in os.environ.items() if k not in ("HERMES_HOST_ID", "HOSTNAME")}
        with patch.dict(os.environ, env, clear=True), \
                patch("socket.gethostname", return_value="MyHost.example.net"):
            from_socket = (oracle_plugin.get_host_id(), ss.get_host_id())
        with patch.dict(os.environ, dict(env, HOSTNAME="myhost.example.net"), clear=True):
            from_env = (oracle_plugin.get_host_id(), ss.get_host_id())
        self.assertEqual(from_socket, ("myhost", "myhost"))
        self.assertEqual(from_env, ("myhost", "myhost"))

    def test_plugin_and_script_share_one_definition(self):
        # The plugin must use the same OWN_SKILL_ROWS predicate as the script.
        src = (REPO / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("_sync.OWN_SKILL_ROWS_SQL", src)
        self.assertNotIn("conv_ref = :ref OR memory_id = :c0", src)

    def test_own_rows_binds_reject_colons(self):
        with self.assertRaises(ValueError):
            ss.own_skill_rows_binds("a:b", "x")
        with self.assertRaises(ValueError):
            ss.own_skill_rows_binds("a", "")
        self.assertEqual(ss.own_skill_rows_binds("h_1", "s%x"), {"pfx": "skill:h\\_1:s\\%x:c%"})


class TestSkillRoots(unittest.TestCase):
    def test_profile_home_from_hermes_constants(self):
        with tempfile.TemporaryDirectory() as td:
            fake = types.ModuleType("hermes_constants")
            setattr(fake, "get_hermes_home", lambda: Path(td) / "profiles" / "work")
            with patch.dict(sys.modules, {"hermes_constants": fake}):
                roots = ss.default_skill_roots()
            self.assertEqual(roots[0], Path(td) / "profiles" / "work" / "skills")
            self.assertEqual(roots[1], Path.home() / ".agents" / "skills")
            self.assertNotIn(Path.home() / "skills", roots)

    def test_hermes_home_env_fallback(self):
        with tempfile.TemporaryDirectory() as td, \
                patch.dict(sys.modules, {"hermes_constants": None}), \
                patch.dict(os.environ, {"HERMES_HOME": td}):
            self.assertEqual(ss.default_skill_roots()[0], Path(td) / "skills")

    def test_lifecycle_ingests_the_profile_copy(self):
        p, conn, cur = _provider()
        cur.fetchone.return_value = None
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            prof = Path(td) / "profile"
            for base, body in ((home / ".hermes" / "skills", "Default"), (prof / "skills", "Profile")):
                d = base / "cat" / "dual"
                d.mkdir(parents=True)
                (d / "SKILL.md").write_text(f"---\nname: dual\ndescription: {body} copy\n---\n\n{body} body\n")
            with patch("pathlib.Path.home", return_value=home), \
                    patch.dict(sys.modules, {"hermes_constants": None}), \
                    patch.dict(os.environ, {"HERMES_HOME": str(prof), "HERMES_HOST_ID": "hosta"}), \
                    patch.object(p, "_start_sync_thread", side_effect=lambda t: (t.run(), True)[1]), \
                    patch.object(p, "_insert_many", return_value=[]) as ins:
                p.on_skill_lifecycle("created", "dual")
        rows = ins.call_args[0][0]
        self.assertTrue(rows[0]["metadata"]["path"].startswith(str(prof)))
        self.assertIn("Profile copy", rows[0]["content"])


class TestLifecycleScheduling(unittest.TestCase):
    def _skill(self, td, name="sk"):
        d = Path(td) / ".hermes" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n\nBody\n")

    def test_caller_thread_does_no_db_or_file_work(self):
        p, conn, cur = _provider()
        started = []
        with tempfile.TemporaryDirectory() as td:
            self._skill(td)
            with patch("pathlib.Path.home", return_value=Path(td)), \
                    patch.dict(sys.modules, {"hermes_constants": None}), \
                    patch.object(p, "_start_sync_thread", side_effect=lambda t: (started.append(t), True)[1]), \
                    patch.object(p, "_skill_ingest_once") as ingest:
                p.on_skill_lifecycle("patched", "sk")
                p._pool.acquire.assert_not_called()
                ingest.assert_not_called()
                self.assertEqual(len(started), 1)
                started[0].run()
                ingest.assert_called_once()

    def test_events_during_a_running_write_are_coalesced(self):
        p, conn, cur = _provider()
        gate = threading.Event()
        entered = threading.Event()
        calls = []

        def slow_ingest(name, roots):
            calls.append(name)
            entered.set()
            if len(calls) == 1:
                gate.wait(5)

        with tempfile.TemporaryDirectory() as td, \
                patch("pathlib.Path.home", return_value=Path(td)), \
                patch.dict(sys.modules, {"hermes_constants": None}), \
                patch.object(p, "_skill_ingest_once", side_effect=slow_ingest):
            p.on_skill_lifecycle("patched", "sk")
            self.assertTrue(entered.wait(5))
            p.on_skill_lifecycle("patched", "sk")  # coalesced
            p.on_skill_lifecycle("edited", "sk")   # coalesced into the same re-run
            self.assertEqual(len(p._sync_threads), 1)
            gate.set()
            for t in list(p._sync_threads):
                t.join(5)
        self.assertEqual(calls, ["sk", "sk"])
        self.assertNotIn(p._lifecycle_key("sk"), oracle_plugin._LIFECYCLE_IN_FLIGHT)
        self.assertNotIn(p._lifecycle_key("sk"), oracle_plugin._LIFECYCLE_PENDING)

    def test_latest_event_wins(self):
        p, conn, cur = _provider()
        gate = threading.Event()
        entered = threading.Event()
        ops = []

        def ingest(name, roots):
            ops.append("ingest")
            entered.set()
            if len(ops) == 1:
                gate.wait(5)

        with tempfile.TemporaryDirectory() as td, \
                patch("pathlib.Path.home", return_value=Path(td)), \
                patch.dict(sys.modules, {"hermes_constants": None}), \
                patch.object(p, "_skill_ingest_once", side_effect=ingest), \
                patch.object(p, "_skill_archive_once", side_effect=lambda n, r: ops.append("archive")):
            p.on_skill_lifecycle("patched", "sk")
            self.assertTrue(entered.wait(5))
            p.on_skill_lifecycle("patched", "sk")
            p.on_skill_lifecycle("archived", "sk")
            gate.set()
            for t in list(p._sync_threads):
                t.join(5)
        self.assertEqual(ops, ["ingest", "archive"])

    def test_pending_event_dropped_after_shutdown_began(self):
        p, conn, cur = _provider()
        gate = threading.Event()
        entered = threading.Event()
        calls = []

        def ingest(name, roots):
            calls.append(name)
            entered.set()
            gate.wait(5)

        with tempfile.TemporaryDirectory() as td, \
                patch("pathlib.Path.home", return_value=Path(td)), \
                patch.dict(sys.modules, {"hermes_constants": None}), \
                patch.object(p, "_skill_ingest_once", side_effect=ingest):
            p.on_skill_lifecycle("patched", "sk")
            self.assertTrue(entered.wait(5))
            p.on_skill_lifecycle("patched", "sk")
            p._write_stop.set()
            gate.set()
            for t in list(p._sync_threads):
                t.join(5)
        self.assertEqual(calls, ["sk"])
        self.assertNotIn(p._lifecycle_key("sk"), oracle_plugin._LIFECYCLE_IN_FLIGHT)

    def test_hash_unchanged_skips_write(self):
        p, conn, cur = _provider()
        with tempfile.TemporaryDirectory() as td:
            self._skill(td)
            info = ss.parse_skill_file(Path(td) / ".hermes" / "skills" / "sk" / "SKILL.md")
            assert info is not None
            cur.fetchone.return_value = (info.hash or info.content_hash,)
            with patch("pathlib.Path.home", return_value=Path(td)), \
                    patch.dict(sys.modules, {"hermes_constants": None}), \
                    patch.dict(os.environ, {"HERMES_HOST_ID": "hosta"}), \
                    patch.object(p, "_start_sync_thread", side_effect=lambda t: (t.run(), True)[1]), \
                    patch.object(p, "_insert_many") as ins:
                p.on_skill_lifecycle("edited", "sk")
        ins.assert_not_called()
        sql, binds = cur.execute.call_args_list[0][0]
        self.assertIn("SELECT json_value(metadata, '$.hash')", sql)
        self.assertEqual(binds, ("skill:hosta:sk:c0",))
        self.assertFalse(any("DELETE" in c[0][0] for c in cur.execute.call_args_list))
        conn.commit.assert_not_called()


class TestMigrateHostIdentity(unittest.TestCase):
    ROOT_A = "/srv/a/.hermes/skills"
    ROOT_B = "/srv/b/.hermes/skills"
    ARCHIVE = "/srv/a/.hermes/archive/skills"

    def _conn(self, legacy, hosted, rowcount=None):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchall.side_effect = [legacy, hosted]
        cur.rowcount = rowcount
        return conn, cur

    def _legacy(self):
        a = f"{self.ROOT_A}/cat/alpha/SKILL.md"
        return [
            ("skill:alpha:c0", "alpha", "alpha", a),
            ("skill:alpha:c1", "alpha", "alpha", a),
            ("skill:delta", "delta", "delta", f"{self.ROOT_A}/delta/SKILL.md"),       # bare id -> :c0
            ("skill:beta:c0", "beta", "beta", f"{self.ARCHIVE}/beta/SKILL.md"),       # archive corpus
            ("skill:gamma:c0", "gamma", "gamma", f"{self.ROOT_A}/old/gamma/SKILL.md"),  # moved on disk
            ("skill:eps:c0", "eps", "eps", f"{self.ROOT_B}/eps/SKILL.md"),            # hosted copy exists
            ("skill:zeta:c0", "zeta", "zeta", f"{self.ROOT_B}/zeta/SKILL.md"),
        ]

    def _listings(self):
        return [
            {"host": "hosta", "skills": [
                {"name": "alpha", "path": f"{self.ROOT_A}/cat/alpha/SKILL.md"},
                {"name": "delta", "path": f"{self.ROOT_A}/delta/SKILL.md"},
                {"name": "gamma", "path": f"{self.ROOT_A}/new/gamma/SKILL.md"},
                {"name": "beta", "path": f"{self.ROOT_A}/beta/SKILL.md"},
            ]},
            {"host": "hostb", "skills": [
                {"name": "eps", "path": f"{self.ROOT_B}/eps/SKILL.md"},
                {"name": "zeta", "path": f"{self.ROOT_B}/zeta/SKILL.md"},
            ]},
        ]

    def test_dry_run_plans_only_live_same_path_skills(self):
        conn, cur = self._conn(self._legacy(), [("skill:hostb:eps:c0",)])
        rep = ss.migrate_host_identity(conn, "T", self._listings(), dry_run=True)
        self.assertEqual(rep["relabelled_skills"], {"hosta": ["alpha", "delta"], "hostb": ["zeta"]})
        self.assertEqual(rep["relabelled_rows"], 4)
        self.assertEqual(rep["superseded"], [{"skill": "eps", "host": "hostb"}])
        self.assertEqual(rep["legacy_skills_total"], 6)
        self.assertEqual(rep["left_shared_skills"], 3)  # beta (archive), gamma (moved), eps
        cur.executemany.assert_not_called()
        conn.commit.assert_not_called()

    def test_run_relabels_and_keeps_reverse_data(self):
        conn, cur = self._conn(self._legacy(), [("skill:hostb:eps:c0",)], rowcount=4)
        ss.migrate_host_identity(conn, "T", self._listings(), dry_run=False)
        sql, plan = cur.executemany.call_args[0]
        self.assertIn("WHERE memory_id = :old_id AND role = 'skill' AND json_value(metadata, '$.host') IS NULL", sql)
        self.assertIn("SET '$.legacy_id' = :old_id", sql)
        by_old = {p["old_id"]: p for p in plan}
        self.assertEqual(set(by_old), {"skill:alpha:c0", "skill:alpha:c1", "skill:delta", "skill:zeta:c0"})
        self.assertEqual(by_old["skill:alpha:c1"]["new_id"], "skill:hosta:alpha:c1")
        self.assertEqual(by_old["skill:delta"]["new_id"], "skill:hosta:delta:c0")
        self.assertEqual(by_old["skill:delta"]["new_ref"], "skill:hosta:delta")
        self.assertEqual(by_old["skill:delta"]["old_ref"], "delta")
        self.assertEqual(by_old["skill:zeta:c0"]["host"], "hostb")
        conn.commit.assert_called_once()

    def test_row_count_mismatch_rolls_back(self):
        conn, cur = self._conn(self._legacy(), [], rowcount=3)
        with self.assertRaises(RuntimeError):
            ss.migrate_host_identity(conn, "T", self._listings(), dry_run=False)
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_same_path_on_two_hosts_is_ambiguous(self):
        legacy = [("skill:alpha:c0", "alpha", "alpha", "/shared/skills/alpha/SKILL.md")]
        listings = [{"host": h, "skills": [{"name": "alpha", "path": "/shared/skills/alpha/SKILL.md"}]}
                    for h in ("hosta", "hostb")]
        conn, cur = self._conn(legacy, [])
        rep = ss.migrate_host_identity(conn, "T", listings, dry_run=True)
        self.assertEqual(rep["ambiguous"], [{"skill": "alpha", "hosts": ["hosta", "hostb"]}])
        self.assertEqual(rep["relabelled_rows"], 0)

    def test_invalid_host_rejected(self):
        conn, cur = self._conn([], [])
        with self.assertRaises(ValueError):
            ss.migrate_host_identity(conn, "T", [{"host": "a:b", "skills": []}], dry_run=True)

    def test_reverse(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchall.side_effect = [
            [("skill:hosta:alpha:c0", "skill:alpha:c0", "alpha"),
             ("skill:hosta:delta:c0", "skill:delta", "delta"),
             ("skill:hosta:dup:c0", "skill:dup:c0", "dup")],
            [("skill:dup:c0",)],  # a legacy row with that id exists again
        ]
        cur.rowcount = 2
        rep = ss.reverse_host_identity(conn, "T", dry_run=False)
        self.assertEqual(rep["restored_rows"], 2)
        self.assertEqual(rep["collisions"], [{"memory_id": "skill:hosta:dup:c0", "legacy_id": "skill:dup:c0"}])
        sql, plan = cur.executemany.call_args[0]
        self.assertIn("REMOVE '$.host'", sql)
        self.assertEqual(plan[1], {"cur_id": "skill:hosta:delta:c0", "old_id": "skill:delta", "old_ref": "delta"})
        conn.commit.assert_called_once()


class TestMigrationCliDefaultsToDryRun(unittest.TestCase):
    def test_migrate_and_reverse_need_apply(self):
        src = (REPO / "scripts" / "sync_skills.py").read_text(encoding="utf-8")
        self.assertIn("dry = args.dry_run or not args.apply", src)
        self.assertIn("reverse_host_identity(conn, table_name, dry_run=dry)", src)
        self.assertIn("migrate_host_identity(conn, table_name, listings, dry_run=dry)", src)


class TestSkillFetchVariants(unittest.TestCase):
    def test_same_path_on_two_hosts_are_two_variants(self):
        p, conn, cur = _provider()
        path = "/srv/skills/x/SKILL.md"
        rows = []
        for h in ("hosta", "hostb"):
            md = json.dumps({"skill_name": "x", "path": path, "host": h, "hash": h})
            rows.append((0, f"[skill_ingest] [skill:x] x — {h}", None, 2, md, f"skill:{h}:x:c0"))
            rows.append((1, f"[skill_ingest] [skill:x] x (chunk 1/1):\n{h} body", None, 2, md, f"skill:{h}:x:c1"))
        cur.fetchall.return_value = rows
        res = json.loads(p.handle_tool_call("oracle_load_skill", {"action": "fetch", "skill": "x"}))
        self.assertEqual(res["status"], "multiple_variants")
        self.assertEqual([(v["host"], v["path"]) for v in res["variants"]], [("hosta", path), ("hostb", path)])
        err = p.handle_tool_call("oracle_load_skill", {"action": "fetch", "skill": "x", "path": path})
        self.assertIn("pass 'host' or 'variant'", err)
        res = json.loads(p.handle_tool_call("oracle_load_skill",
                                            {"action": "fetch", "skill": "x", "path": path, "host": "hostb"}))
        self.assertEqual(res["status"], "ok")
        self.assertIn("hostb body", res["markdown"])
        self.assertNotIn("hosta body", res["markdown"])

    def test_variants_with_oracle_numbers_serialise(self):
        import decimal

        p, conn, cur = _provider()
        rows = [(0, "[skill_ingest] [skill:x] x", None, None,
                 {"skill_name": "x", "path": f"/s/{h}/x/SKILL.md", "host": "", "mtime": decimal.Decimal("1731600000.5")},
                 f"skill:x{h}:c0") for h in ("a", "b")]
        cur.fetchall.return_value = rows
        res = json.loads(p.handle_tool_call("oracle_load_skill", {"action": "fetch", "skill": "x"}))
        self.assertEqual(res["status"], "multiple_variants")
        self.assertEqual(res["variants"][0]["mtime"], 1731600000.5)

    def test_fetch_is_bounded_in_sql(self):
        p, conn, cur = _provider()
        cur.fetchall.return_value = []
        p.handle_tool_call("oracle_load_skill", {"action": "fetch", "skill": "x"})
        sql, binds = cur.execute.call_args[0][0], cur.execute.call_args[0][1]
        self.assertIn("WHERE rn <= :cap", sql)
        self.assertEqual(binds["cap"], oracle_plugin._SKILL_FETCH_MAX_CHUNKS + 2)
        self.assertIs(cur.execute.call_args.kwargs.get("fetch_lobs"), False)

    def test_schema_has_host(self):
        schemas = {s["name"]: s for s in oracle_plugin.OracleMemoryProvider().get_tool_schemas()}
        props = schemas["oracle_load_skill"]["parameters"]["properties"]
        self.assertIn("host", props)


class TestSyncSkillsTool(unittest.TestCase):
    def test_prune_and_dry_run_reach_the_library(self):
        p = oracle_plugin.OracleMemoryProvider()
        p._pool = MagicMock()
        mod = MagicMock()
        mod.sync_skills_library.return_value = {"status": "success"}
        with patch.object(oracle_plugin, "_sync_skills_module", return_value=mod), \
                patch.dict(os.environ, {"HERMES_HOST_ID": "hosta"}):
            p.handle_tool_call("oracle_sync_skills", {"prune": True, "dry_run": True, "prune_limit": 7})
        kw = mod.sync_skills_library.call_args.kwargs
        self.assertEqual((kw["prune"], kw["dry_run"], kw["prune_limit"], kw["host_tag"]), (True, True, 7, "hosta"))

    def test_prune_limit_bounds(self):
        p = oracle_plugin.OracleMemoryProvider()
        p._pool = MagicMock()
        for bad in (0, 501, "x"):
            self.assertIn("prune_limit", p.handle_tool_call("oracle_sync_skills", {"prune_limit": bad}))

    def test_bulk_prune_scans_the_default_roots(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        provider = MagicMock()
        provider._table = "T"
        provider._pool.acquire.return_value.__enter__.return_value = conn
        cur.fetchall.return_value = []
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "skills").mkdir()
            with patch.dict(sys.modules, {"hermes_constants": None}), \
                    patch.dict(os.environ, {"HERMES_HOME": td}), \
                    patch("pathlib.Path.home", return_value=Path(td) / "nohome"):
                ss.sync_skills_library(provider=provider, prune=True, host_tag="hosta")
        stale = [c[0] for c in cur.execute.call_args_list if "memory_id LIKE :own" in c[0][0]]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0][1]["r0"], ss.like_escape(str((Path(td) / "skills").resolve()) + os.sep) + "%")


if __name__ == "__main__":
    unittest.main()
