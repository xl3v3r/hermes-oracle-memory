"""1.1.1.4 ownership guards (#5, #6, #7, #11): generic tools never delete skill
rows, the lifecycle hook stores a skill under its parsed name and never
replaces or deletes a copy whose SKILL.md is live in another profile, writers
are coalesced per process, a backfill never writes a vector for outdated text,
and the bulk sync only trusts its own or the shared corpus' copy. Fully
mocked; no database."""

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
from scripts import sync_skills as ss  # noqa: E402


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
    return p, conn, cur


def _sqls(cur):
    return [str(c[0][0]) for c in cur.execute.call_args_list]


def _write_skill(base: Path, dirname: str, name: str, body: str = "Body") -> Path:
    d = base / dirname
    d.mkdir(parents=True)
    f = d / "SKILL.md"
    f.write_text(f"---\nname: {name}\ndescription: d\n---\n\n{body}\n", encoding="utf-8")
    return f


class TestDeleteNeverTouchesSkillRows(unittest.TestCase):
    def test_skill_id_refused_without_db_call(self):
        p, _, cur = _provider()
        for mid in ("skill:hostb:foo:c0", "skill:foo", "SKILL:foo:c3"):
            out = p.handle_tool_call("oracle_delete", {"memory_id": mid})
            self.assertEqual(out, oracle_plugin.tool_error(oracle_plugin._SKILL_ROW_READONLY))
        cur.execute.assert_not_called()

    def test_delete_sql_excludes_skill_role(self):
        p, _, cur = _provider(rowcount=1)
        out = p.handle_tool_call("oracle_delete", {"memory_id": "fact_1"})
        self.assertIn('"deleted"', out)
        delete = [s for s in _sqls(cur) if s.lstrip().upper().startswith("DELETE")]
        self.assertEqual(len(delete), 1)
        self.assertIn("<> 'skill'", delete[0])

    def test_skill_row_under_other_id_reports_read_only(self):
        p, _, cur = _provider(rowcount=0)
        with patch.object(p, "_is_skill_row", return_value=True):
            out = p.handle_tool_call("oracle_delete", {"memory_id": "odd-skill-row"})
        self.assertEqual(out, oracle_plugin.tool_error(oracle_plugin._SKILL_ROW_READONLY))


class TestLifecycleIdentity(unittest.TestCase):
    def _run(self, p, action, name, home):
        with patch("pathlib.Path.home", return_value=home), \
                patch.dict(sys.modules, {"hermes_constants": None}), \
                patch.dict(os.environ, {"HERMES_HOST_ID": "hosta"}), \
                patch.object(p, "_start_sync_thread", side_effect=lambda t: (t.run(), True)[1]):
            p.on_skill_lifecycle(action, name)

    def test_frontmatter_name_is_the_identity(self):
        p, conn, cur = _provider()
        cur.fetchone.return_value = None
        with tempfile.TemporaryDirectory() as td:
            _write_skill(Path(td) / ".hermes" / "skills", "dir-name", "front-name")
            with patch.object(p, "_insert_many", return_value=[]) as ins:
                self._run(p, "edited", "dir-name", Path(td))
        rows = ins.call_args[0][0]
        self.assertTrue(all(r["memory_id"].startswith("skill:hosta:front-name:c") for r in rows))
        hash_sel = cur.execute.call_args_list[0]
        self.assertEqual(hash_sel[0][1], ("skill:hosta:front-name:c0",))
        delete = [c for c in cur.execute.call_args_list if str(c[0][0]).lstrip().upper().startswith("DELETE")]
        self.assertEqual(delete[0][0][1]["pfx"], ss.like_escape("skill:hosta:front-name:c") + "%")

    def test_invalid_frontmatter_name_not_stored(self):
        p, conn, cur = _provider()
        with tempfile.TemporaryDirectory() as td:
            _write_skill(Path(td) / ".hermes" / "skills", "okdir", "bad:name")
            with patch.object(p, "_insert_many", return_value=[]) as ins:
                self._run(p, "edited", "okdir", Path(td))
        ins.assert_not_called()
        cur.execute.assert_not_called()

    def test_invalid_event_names_ignored_on_caller_thread(self):
        p, conn, cur = _provider()
        started = []
        with patch.object(p, "_start_sync_thread", side_effect=lambda t: (started.append(t), True)[1]):
            for bad in ("../etc", "a*", "x/y", "[ab]", "", "a:b"):
                p.on_skill_lifecycle("edited", bad)
                p.on_skill_lifecycle("archived", bad)
        self.assertEqual(started, [])

    def test_copy_live_in_another_profile_is_not_replaced(self):
        p, conn, cur = _provider()
        with tempfile.TemporaryDirectory() as td:
            other = _write_skill(Path(td) / "other-profile" / "skills", "shared", "shared", "Other")
            _write_skill(Path(td) / ".hermes" / "skills", "shared", "shared", "Mine")
            cur.fetchone.return_value = ("old-hash", str(other))
            with patch.object(p, "_insert_many", return_value=[]) as ins:
                self._run(p, "edited", "shared", Path(td))
        ins.assert_not_called()
        self.assertFalse(any(s.lstrip().upper().startswith("DELETE") for s in _sqls(cur)))

    def test_copy_whose_file_is_gone_is_replaced(self):
        p, conn, cur = _provider()
        with tempfile.TemporaryDirectory() as td:
            _write_skill(Path(td) / ".hermes" / "skills", "moved", "moved")
            cur.fetchone.return_value = ("old-hash", str(Path(td) / "gone" / "moved" / "SKILL.md"))
            with patch.object(p, "_insert_many", return_value=[]) as ins:
                self._run(p, "edited", "moved", Path(td))
        ins.assert_called_once()

    def test_archive_keeps_copy_live_in_another_profile(self):
        p, conn, cur = _provider()
        with tempfile.TemporaryDirectory() as td:
            other = _write_skill(Path(td) / "other-profile" / "skills", "arch", "arch")
            (Path(td) / ".hermes" / "skills").mkdir(parents=True)
            cur.fetchall.return_value = [("skill:hosta:arch:c0", "arch", str(other))]
            self._run(p, "archived", "arch", Path(td))
        self.assertFalse(any(s.lstrip().upper().startswith("DELETE") for s in _sqls(cur)))

    def test_archive_finds_copy_by_stored_path(self):
        p, conn, cur = _provider()
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".hermes" / "skills").mkdir(parents=True)
            gone = Path(td) / ".hermes" / "skills" / "dir-name" / "SKILL.md"
            cur.fetchall.return_value = [("skill:hosta:front-name:c0", "front-name", str(gone))]
            self._run(p, "archived", "dir-name", Path(td))
        sel = cur.execute.call_args_list[0][0]
        self.assertEqual(sel[1]["c0"], "skill:hosta:dir-name:c0")
        self.assertEqual(sel[1]["own"], ss.like_escape("skill:hosta:") + "%")
        self.assertTrue(sel[1]["tail"].endswith(ss.like_escape(f"{os.sep}dir-name{os.sep}SKILL.md")))
        deletes = [c[0][1] for c in cur.execute.call_args_list if str(c[0][0]).lstrip().upper().startswith("DELETE")]
        self.assertEqual(deletes, [{"pfx": ss.like_escape("skill:hosta:front-name:c") + "%"}])


class TestWritersCoalescedPerProcess(unittest.TestCase):
    def test_two_instances_share_one_writer_per_skill(self):
        a, _, _ = _provider()
        b, _, _ = _provider()
        gate, entered = threading.Event(), threading.Event()
        calls = []

        def ingest_a(name, roots):
            calls.append(("a", name))
            entered.set()
            gate.wait(5)

        with tempfile.TemporaryDirectory() as td, \
                patch("pathlib.Path.home", return_value=Path(td)), \
                patch.dict(sys.modules, {"hermes_constants": None}), \
                patch.object(a, "_skill_ingest_once", side_effect=ingest_a), \
                patch.object(b, "_skill_ingest_once", side_effect=lambda n, r: calls.append(("b", n))):
            a.on_skill_lifecycle("edited", "same")
            self.assertTrue(entered.wait(5))
            b.on_skill_lifecycle("edited", "same")  # coalesced, not a second writer
            self.assertEqual(b._sync_threads, [])
            gate.set()
            for t in list(a._sync_threads):
                t.join(5)
        self.assertEqual(calls, [("a", "same"), ("b", "same")])
        self.assertNotIn(a._lifecycle_key("same"), oracle_plugin._LIFECYCLE_IN_FLIGHT)


class TestBackfillNeverWritesOutdatedVectors(unittest.TestCase):
    def test_pending_entry_carries_latest_content(self):
        p, _, _ = _provider()
        with patch.object(p, "_ensure_embed_worker"):
            p._queue_embed_backfill("m1", "old text")
            p._queue_embed_backfill("m2", "other")
            p._queue_embed_backfill("m1", "new text")
        self.assertEqual(list(p._embed_pending), [("m1", "new text"), ("m2", "other")])

    def test_update_guarded_by_null_embedding_and_same_content(self):
        p, conn, cur = _provider()
        p._embed_pending.extend([("m1", "text one"), ("m2", "text two")])
        conn.createlob.side_effect = lambda typ, text: ("CLOB", text)
        with patch.object(p, "_embed_many_tagged", return_value=[([0.1], "M"), ([0.2], "M")]):
            p._embed_worker_loop()  # inline: returns once the queue is empty
        cur.executemany.assert_called_once()
        conn.commit.assert_called_once()
        sql, binds = cur.executemany.call_args[0]
        self.assertIn("embedding IS NULL", sql)
        self.assertIn("DBMS_LOB.COMPARE(content, :4) = 0", sql)
        # the text is bound as a temporary CLOB (a str over 32,767 bytes fails in SQL)
        self.assertEqual([(b[2], b[3]) for b in binds], [("m1", ("CLOB", "text one")), ("m2", ("CLOB", "text two"))])
        self.assertEqual(list(p._embed_pending), [])


class TestBulkSyncTrustsOnlyOwnOrSharedCopies(unittest.TestCase):
    def _sync(self, rows, mode="meta"):
        provider = MagicMock()
        provider._table = "HERMES_AGENT_MEMORY"
        conn, cur = MagicMock(), MagicMock()
        provider._pool.acquire.return_value.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        with tempfile.TemporaryDirectory() as td:
            f = _write_skill(Path(td), "demo", "demo")
            skill = ss.parse_skill_file(f)
            assert skill is not None
            h = skill.hash or skill.content_hash
            cur.fetchall.return_value = [tuple(h if v == "<H>" else v for v in r) for r in rows]
            res = ss.sync_skills_library(provider=provider, mode=mode, dirs=[td], host_tag="hosta")
        return res, provider

    def test_other_hosts_identical_copy_does_not_skip(self):
        res, prov = self._sync([("skill:hostb:demo:c0", "demo", "<H>", "/x/demo/SKILL.md", "hostb", None)])
        self.assertEqual(res["indexed"], 1)
        self.assertEqual(prov._insert_many.call_count, 1)

    def test_shared_corpus_identical_copy_skips(self):
        res, prov = self._sync([("skill:demo:c0", "demo", "<H>", "/x/demo/SKILL.md", None, None)])
        self.assertEqual(res["skipped"], 1)
        prov._insert_many.assert_not_called()

    def test_full_sync_replaces_own_meta_only_copy(self):
        res, prov = self._sync([("skill:hosta:demo:c0", "demo", "<H>", "/x/demo/SKILL.md", "hosta", None)], mode="full")
        self.assertEqual(res["mode"], "full")  # the keyword mode is honoured
        self.assertEqual(res["updated"], 1)
        rows = prov._insert_many.call_args[0][0]
        self.assertIn("total_chunks", rows[0]["metadata"])
        res, prov = self._sync([("skill:hosta:demo:c0", "demo", "<H>", "/x/demo/SKILL.md", "hosta", 3)], mode="full")
        self.assertEqual(res["skipped"], 1)


class TestListFilterIsLiteral(unittest.TestCase):
    def test_wildcards_escaped(self):
        p, conn, cur = _provider()
        cur.fetchall.return_value = []
        p._load_skill_list("a_b%c")
        sql, binds = cur.execute.call_args[0][0], cur.execute.call_args[0][1]
        self.assertIn("ESCAPE", sql)
        self.assertEqual(binds["f"], "%" + ss.like_escape("a_b%c") + "%")


if __name__ == "__main__":
    unittest.main()
