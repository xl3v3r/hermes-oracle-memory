"""1.1.1.5 follow-ups (#14): the bulk sync judges ownership against the
profile's own skill roots and can be told to replace a foreign copy, the
lifecycle lookup treats nothing under ignored trees (.archive/ ...) as live and
walks any depth without looping, and the embedding backfill neither drops rows
silently nor re-queues them once shutdown has begun. Fully mocked; no database."""

import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401  (stubs agent.* / tools.*)
import __init__ as oracle_plugin  # noqa: E402
from scripts import sync_skills as ss  # noqa: E402
from tests.test_skill_ownership import _link_in_tmp, _provider, _sqls, _write_skill  # noqa: E402


@contextmanager
def _home(home: Path):
    """Run as a profile whose home is *home*: default roots are
    <home>/.hermes/skills and <home>/.agents/skills, host id 'hosta'."""
    with ExitStack() as st:
        st.enter_context(patch("pathlib.Path.home", return_value=home))
        st.enter_context(patch.dict(sys.modules, {"hermes_constants": None}))
        st.enter_context(patch.dict(os.environ, {"HERMES_HOST_ID": "hosta"}))
        os.environ.pop("HERMES_HOME", None)
        yield


def _bulk(home: Path, dirs, stored_path: str, **kw):
    provider = MagicMock()
    provider._table = "HERMES_AGENT_MEMORY"
    conn, cur = MagicMock(), MagicMock()
    provider._pool.acquire.return_value.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = [("skill:hosta:demo:c0", "demo", "old-hash", stored_path, "hosta", 2)]
    with _home(home):
        res = ss.sync_skills_library(provider=provider, mode="meta", dirs=dirs, host_tag="hosta", **kw)
    return res, provider


class TestForeignCopiesJudgedByProfileRoots(unittest.TestCase):
    def test_explicit_dirs_subset_replaces_copy_from_profiles_other_root(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            mine = _write_skill(home / ".hermes" / "skills", "demo", "demo", "Skills dir")
            _write_skill(home / ".agents" / "skills", "demo", "demo", "Agents dir")
            res, prov = _bulk(home, [str(home / ".agents" / "skills")], str(mine))
        self.assertEqual(res["skipped_foreign"], 0)
        self.assertEqual(res["updated"], 1)
        prov._insert_many.assert_called_once()

    def test_dirs_outside_defaults_keep_live_default_copy(self):
        # A copy stored from the extra dir would be foreign to every later
        # default sync and to the lifecycle hook, so the default copy stays.
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            mine = _write_skill(home / ".hermes" / "skills", "demo", "demo", "Default root")
            _write_skill(home / "extra", "demo", "demo", "Extra root")
            res, prov = _bulk(home, [str(home / "extra")], str(mine))
            forced, prov2 = _bulk(home, [str(home / "extra")], str(mine), replace_foreign=True)
        self.assertEqual(res["skipped_foreign"], 1)
        self.assertIn("replace_foreign", res.get("hint", ""))
        prov._insert_many.assert_not_called()
        self.assertEqual((forced["skipped_foreign"], forced["updated"]), (0, 1))
        prov2._insert_many.assert_called_once()

    def test_dirs_listing_outside_dir_first_keep_default_copy_with_accurate_hint(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            mine = _write_skill(home / ".hermes" / "skills", "demo", "demo", "Default root")
            _write_skill(home / "extra", "demo", "demo", "Extra root")
            res, prov = _bulk(home, [str(home / "extra"), str(home / ".hermes" / "skills")], str(mine))
        self.assertEqual(res["skipped_foreign"], 1)
        prov._insert_many.assert_not_called()
        self.assertNotIn("does not scan", res["hint"])
        self.assertIn("outside the default", res["hint"])
        # replace_foreign would recreate the problem the rule prevents.
        self.assertIn("without --dirs", res["hint"])

    def test_dirs_outside_defaults_replace_gone_default_copy(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / ".hermes" / "skills").mkdir(parents=True)
            _write_skill(home / "extra", "demo", "demo", "Extra root")
            gone = home / ".hermes" / "skills" / "demo" / "SKILL.md"
            res, prov = _bulk(home, [str(home / "extra")], str(gone))
        self.assertEqual((res["skipped_foreign"], res["updated"]), (0, 1))
        prov._insert_many.assert_called_once()

    def test_copy_from_removed_root_kept_with_hint(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            old = _write_skill(home / "old-root", "demo", "demo", "Old root")
            _write_skill(home / ".hermes" / "skills", "demo", "demo", "Now")
            res, prov = _bulk(home, None, str(old))
        self.assertEqual(res["skipped_foreign"], 1)
        self.assertIn("replace_foreign", res.get("hint", ""))
        self.assertNotIn("without --dirs", res["hint"])
        prov._insert_many.assert_not_called()

    def test_replace_foreign_replaces_copy_from_removed_root(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            old = _write_skill(home / "old-root", "demo", "demo", "Old root")
            _write_skill(home / ".hermes" / "skills", "demo", "demo", "Now")
            res, prov = _bulk(home, None, str(old), replace_foreign=True)
        self.assertEqual(res["skipped_foreign"], 0)
        self.assertEqual(res["updated"], 1)
        self.assertNotIn("hint", res)
        prov._insert_many.assert_called_once()

    def test_provider_sync_skills_passes_replace_foreign(self):
        p, _, _ = _provider()
        lib = MagicMock(return_value={})
        with patch.object(oracle_plugin, "_sync_skills_module", return_value=MagicMock(sync_skills_library=lib)):
            p.sync_skills(replace_foreign=True)
        self.assertIs(lib.call_args.kwargs["replace_foreign"], True)

    def test_tool_replace_foreign_param(self):
        p = oracle_plugin.OracleMemoryProvider()
        p._pool = MagicMock()
        schema = next(s for s in p.get_tool_schemas() if s["name"] == "oracle_sync_skills")
        self.assertEqual(schema["parameters"]["properties"]["replace_foreign"]["type"], "boolean")
        mod = MagicMock()
        mod.sync_skills_library.return_value = {"status": "success"}
        with patch.object(oracle_plugin, "_sync_skills_module", return_value=mod):
            for args, want in (({"replace_foreign": True}, True), ({}, False), ({"replace_foreign": "false"}, False)):
                p.handle_tool_call("oracle_sync_skills", args)
                self.assertIs(mod.sync_skills_library.call_args.kwargs["replace_foreign"], want, args)

    def test_cli_replace_foreign_flag(self):
        lib = MagicMock(return_value={})
        prov = MagicMock()
        prov.is_available.return_value = True
        plugin = MagicMock()
        plugin.OracleMemoryProvider.return_value = prov
        with patch.object(sys, "argv", ["sync_skills.py", "--replace-foreign"]), \
                patch.object(ss._load_common(), "load_plugin", return_value=plugin), \
                patch.object(ss, "sync_skills_library", lib), \
                patch("builtins.print"):
            ss.main()
        self.assertIs(lib.call_args.kwargs["replace_foreign"], True)

    def test_cli_table_option_reaches_the_provider(self):
        # The provider reads the table from the settings in initialize(), so
        # an attribute set before that call was overwritten.
        seen = {}
        prov = MagicMock()
        prov.is_available.return_value = True
        prov.initialize.side_effect = lambda *a, **k: seen.setdefault("table", os.environ.get("HERMES_ORACLE_TABLE"))
        plugin = MagicMock()
        plugin.OracleMemoryProvider.return_value = prov
        with patch.object(sys, "argv", ["sync_skills.py", "--table", "OTHER_TABLE"]), \
                patch.object(ss._load_common(), "load_plugin", return_value=plugin), \
                patch.object(ss, "sync_skills_library", MagicMock(return_value={})), \
                patch.dict(os.environ), patch("builtins.print"):
            ss.main()
        self.assertEqual(seen["table"], "OTHER_TABLE")


class TestLifecycleLookup(unittest.TestCase):
    _find = staticmethod(oracle_plugin.OracleMemoryProvider._find_skill_file)

    def test_link_into_archive_is_not_live(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "skills"
            archived = _write_skill(root / ".archive", "foo", "foo")
            _link_in_tmp(root / "foo", archived.parent)
            (root / "cat").mkdir()
            _link_in_tmp(root / "cat" / "foo", archived.parent)
            self.assertIsNone(self._find([root], "foo"))

    def test_archive_event_drops_copy_whose_path_leads_into_archive(self):
        p, conn, cur = _provider()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".hermes" / "skills"
            archived = _write_skill(root / ".archive", "foo", "foo")
            _link_in_tmp(root / "foo", archived.parent)
            cur.fetchall.return_value = [("skill:hosta:foo:c0", "foo", str(root / "foo" / "SKILL.md"))]
            with _home(Path(td)), patch.object(p, "_start_sync_thread", side_effect=lambda t: (t.run(), True)[1]):
                p.on_skill_lifecycle("archived", "foo")
        deletes = [s for s in _sqls(cur) if s.lstrip().upper().startswith("DELETE")]
        self.assertEqual(len(deletes), 1)

    def test_skill_found_at_any_depth(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "skills"
            deep = root.joinpath("a", "b", "c", "d", "e", "f", "g", "h")
            f = _write_skill(deep, "deepest", "deepest")
            self.assertEqual(self._find([root], "deepest"), f)

    def test_link_loop_terminates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "skills"
            (root / "a").mkdir(parents=True)
            _link_in_tmp(root / "a" / "loop", root / "a")
            _link_in_tmp(root / "back", root)
            self.assertIsNone(self._find([root], "missing"))


class TestLoopSafeWalk(unittest.TestCase):
    def _looped_root(self, td):
        # Resolved: discovery reports paths under the resolved root, and the
        # temp dir is itself a link on macOS (/var -> /private/var).
        root = Path(td).resolve() / "skills"
        f = _write_skill(root / "a", "inner", "inner")
        _link_in_tmp(root / "a" / "loop", root / "a")
        _link_in_tmp(root / "back", root)
        return root, f

    def test_walker_visits_each_real_dir_once(self):
        with tempfile.TemporaryDirectory() as td:
            root, _ = self._looped_root(td)
            dirs = [Path(d) for d, _sub, _files in ss.walk_skill_tree(root)]
        self.assertEqual(sorted(d.name for d in dirs), ["a", "inner", "skills"])

    def test_discovery_terminates_on_link_loops(self):
        import threading

        with tempfile.TemporaryDirectory() as td:
            root, f = self._looped_root(td)
            out = []
            t = threading.Thread(target=lambda: out.append(ss.discover_skills(dirs=[root])), daemon=True)
            t.start()
            t.join(timeout=20)
            self.assertFalse(t.is_alive(), "discovery did not finish")
        self.assertEqual([(s.name, s.path) for s in out[0]], [("inner", f)])


class TestBackfillEdges(unittest.TestCase):
    def test_rowcounts_unavailable_rereads_whole_group(self):
        p, conn, cur = _provider()
        p._embed_pending.extend([("m1", "text one"), ("m2", "text two")])
        cur.getarraydmlrowcounts.side_effect = RuntimeError("driver cannot report row counts")
        # m1 got its vector; m2's stored text differs and it still has none.
        cur.fetchall.side_effect = [[("m2", "stored two")], []]
        texts = []

        def embed(batch):
            texts.extend(batch)
            return [([0.1], "M") for _ in batch]

        with patch.object(p, "_embed_many_tagged", side_effect=embed):
            p._embed_worker_loop()
        reread = [c for c in cur.execute.call_args_list if "IN (" in str(c[0][0])]
        self.assertEqual(sorted(reread[0][0][1]), ["m1", "m2"])
        self.assertIn("stored two", texts)
        self.assertEqual(list(p._embed_pending), [])

    def test_no_reread_or_requeue_after_shutdown_began(self):
        p, conn, cur = _provider()
        p._embed_pending.append(("m1", "queued text"))
        cur.getarraydmlrowcounts.return_value = [0]
        cur.fetchall.return_value = [("m1", "stored text")]
        p._write_stop.set()
        with patch.object(p, "_embed_many_tagged", return_value=[([0.1], "M")]) as emb, \
                self.assertLogs(oracle_plugin.logger, "INFO") as logs:
            p._embed_worker_loop()
        emb.assert_called_once()
        self.assertFalse([c for c in cur.execute.call_args_list if "IN (" in str(c[0][0])])
        self.assertEqual(list(p._embed_pending), [])
        self.assertNotIn("m1", p._embed_attempts)
        self.assertTrue(any("next startup" in m for m in logs.output))


if __name__ == "__main__":
    unittest.main()
