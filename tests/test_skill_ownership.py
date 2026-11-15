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


def _link_in_tmp(link: Path, target: Path) -> None:
    """The only way these tests create a symlink: link and target must both lie
    in the temp dir (never the installed plugin; see
    test_suite_never_writes_outside_checkout)."""
    tmp = Path(tempfile.gettempdir()).resolve()
    for p in (link.parent, target):
        Path(p).resolve().relative_to(tmp)  # raises outside the temp dir
    os.symlink(target, link)


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


class TestArchiveAndPathsRound2(unittest.TestCase):
    _run = TestLifecycleIdentity._run

    def test_archive_keeps_live_in_root_copy_under_archived_name(self):
        # dir 'baz' (live) has frontmatter name 'foo'; dir 'foo' was archived
        p, conn, cur = _provider()
        with tempfile.TemporaryDirectory() as td:
            live = _write_skill(Path(td) / ".hermes" / "skills", "baz", "foo")
            cur.fetchall.return_value = [("skill:hosta:foo:c0", "foo", str(live))]
            self._run(p, "archived", "foo", Path(td))
        self.assertFalse(any(s.lstrip().upper().startswith("DELETE") for s in _sqls(cur)))

    def test_archive_keeps_live_deeply_nested_copy(self):
        p, conn, cur = _provider()
        with tempfile.TemporaryDirectory() as td:
            live = _write_skill(Path(td) / ".hermes" / "skills" / "cat" / "sub", "deep", "deep2")
            cur.fetchall.return_value = [("skill:hosta:deep2:c0", "deep2", str(live))]
            self._run(p, "archived", "other", Path(td))
        self.assertFalse(any(s.lstrip().upper().startswith("DELETE") for s in _sqls(cur)))

    def test_deeply_nested_skill_found_for_ingest(self):
        p, conn, cur = _provider()
        cur.fetchone.return_value = None
        with tempfile.TemporaryDirectory() as td:
            _write_skill(Path(td) / ".hermes" / "skills" / "a" / "b", "nested", "nested")
            (Path(td) / ".hermes" / "skills" / ".archive" / "x").mkdir(parents=True)
            with patch.object(p, "_insert_many", return_value=[]) as ins:
                self._run(p, "edited", "nested", Path(td))
        ins.assert_called_once()

    def test_archived_symlinked_skill_is_removed(self):
        p, conn, cur = _provider()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".hermes" / "skills"
            root.mkdir(parents=True)
            target = _write_skill(Path(td) / "elsewhere", "linked", "linked")
            _link_in_tmp(root / "linked", target.parent)
            info = ss.parse_skill_file(root / "linked" / "SKILL.md")
            assert info is not None
            self.assertEqual(str(info.path), str(root / "linked" / "SKILL.md"))  # in-root path kept
            # Hermes archives the link itself; the target stays.
            (root / ".archive").mkdir()
            (root / "linked").rename(root / ".archive" / "linked")
            for stored in (str(root / "linked" / "SKILL.md"), str(target.resolve())):  # new row, pre-1.1.1.4 row
                cur.reset_mock()
                cur.fetchall.return_value = [("skill:hosta:linked:c0", "linked", stored)]
                self._run(p, "archived", "linked", Path(td))
                deletes = [s for s in _sqls(cur) if s.lstrip().upper().startswith("DELETE")]
                self.assertEqual(len(deletes), 1, stored)

    def test_ingest_rewrites_meta_only_or_moved_copy(self):
        with tempfile.TemporaryDirectory() as td:
            f = _write_skill(Path(td) / ".hermes" / "skills", "mv", "mv")
            info = ss.parse_skill_file(f)
            assert info is not None
            h = info.hash or info.content_hash
            for row, expect in (
                ((h, str(f), 2), False),  # current: same hash, path, full
                ((h, str(f), None), True),  # meta-only copy
                ((h, str(Path(td) / "gone" / "mv" / "SKILL.md"), 2), True),  # recorded path is gone
            ):
                p, conn, cur = _provider()
                cur.fetchone.return_value = row
                with patch.object(p, "_insert_many", return_value=[]) as ins:
                    self._run(p, "edited", "mv", Path(td))
                self.assertEqual(ins.called, expect, row)


class TestBulkSyncProfileGuard(unittest.TestCase):
    def _sync(self, stored_path, force=False):
        provider = MagicMock()
        provider._table = "HERMES_AGENT_MEMORY"
        conn, cur = MagicMock(), MagicMock()
        provider._pool.acquire.return_value.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as other:
            _write_skill(Path(td), "demo", "demo", "Mine")
            theirs = _write_skill(Path(other), "demo", "demo", "Theirs")
            path = {"<THEIRS>": str(theirs)}.get(stored_path, stored_path)
            cur.fetchall.return_value = [("skill:hosta:demo:c0", "demo", "old-hash", path, "hosta", 2)]
            res = ss.sync_skills_library(provider=provider, mode="meta", dirs=[td], host_tag="hosta", force=force)
        return res, provider

    def test_copy_live_in_another_profile_is_not_replaced(self):
        for force in (False, True):
            res, prov = self._sync("<THEIRS>", force=force)
            self.assertEqual(res["skipped_foreign"], 1)
            self.assertEqual(res["foreign_skills"][0]["name"], "demo")
            prov._insert_many.assert_not_called()
            self.assertEqual(res["indexed"] + res["updated"], 0)

    def test_copy_whose_file_is_gone_is_replaced(self):
        res, prov = self._sync("/nonexistent/demo/SKILL.md")
        self.assertEqual(res["skipped_foreign"], 0)
        self.assertEqual(res["updated"], 1)


class TestPathHelpers(unittest.TestCase):
    def test_copy_is_current(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            out = Path(td) / "out"
            real = _write_skill(out, "s", "s")
            root.mkdir()
            _link_in_tmp(root / "s", real.parent)
            link = root / "s" / "SKILL.md"
            self.assertTrue(ss.copy_is_current("h", str(link), "h", link, [root]))
            self.assertFalse(ss.copy_is_current("x", str(link), "h", link, [root]))
            self.assertTrue(ss.copy_is_current("h", None, "h", link, [root]))
            # pre-1.1.1.4 row with the resolved target outside the roots: rewritten once
            self.assertFalse(ss.copy_is_current("h", str(real.resolve()), "h", link, [root]))
            # the target inside the roots (shared root reached through a link): current
            self.assertTrue(ss.copy_is_current("h", str(real.resolve()), "h", link, [root, out]))
            self.assertFalse(ss.copy_is_current("h", str(Path(td) / "gone" / "SKILL.md"), "h", link, [root]))

    def test_listing_matches_legacy_resolved_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            real = _write_skill(Path(td) / "out", "s", "s")
            root.mkdir()
            _link_in_tmp(root / "s", real.parent)
            listing = ss.list_local_skills([str(root)])
        entry = listing["skills"][0]
        self.assertEqual(entry["path"], str(root.resolve() / "s" / "SKILL.md"))
        self.assertEqual(entry["real_path"], str(real.resolve()))


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
            for t in list(b._sync_threads):  # the coalesced event runs on b's own thread
                t.join(5)
        self.assertEqual(calls, [("a", "same"), ("b", "same")])
        self.assertNotIn(a._lifecycle_key("same"), oracle_plugin._LIFECYCLE_IN_FLIGHT)

    def test_key_fixed_at_scheduling_time(self):
        p, _, _ = _provider()
        p._pool_fingerprint = "fp1"
        held = []
        with patch.object(p, "_start_sync_thread", side_effect=lambda t: (held.append(t), True)[1]), \
                patch.object(p, "_skill_ingest_once"):
            p.on_skill_lifecycle("edited", "fp-skill")
            p._pool_fingerprint = "fp2"  # re-initialised before the writer ran
            held[0].run()
        self.assertNotIn(("fp1", p._table, "fp-skill"), oracle_plugin._LIFECYCLE_IN_FLIGHT)
        self.assertNotIn(("fp2", p._table, "fp-skill"), oracle_plugin._LIFECYCLE_IN_FLIGHT)

    def test_event_for_live_provider_survives_other_shutdown(self):
        a, _, _ = _provider()
        b, _, _ = _provider()
        gate, entered = threading.Event(), threading.Event()
        calls = []

        def ingest_a(name, roots):
            entered.set()
            gate.wait(5)

        with tempfile.TemporaryDirectory() as td, \
                patch("pathlib.Path.home", return_value=Path(td)), \
                patch.dict(sys.modules, {"hermes_constants": None}), \
                patch.object(a, "_skill_ingest_once", side_effect=ingest_a), \
                patch.object(b, "_skill_ingest_once", side_effect=lambda n, r: calls.append(("b", n))):
            a.on_skill_lifecycle("edited", "handoff")
            self.assertTrue(entered.wait(5))
            b.on_skill_lifecycle("edited", "handoff")
            a._write_stop.set()  # a shuts down; b's event must still run, on b
            gate.set()
            for t in list(a._sync_threads):
                t.join(5)
            for t in list(b._sync_threads):
                t.join(5)
            self.assertEqual(len(b._sync_threads), 1)  # tracked by b, so b.shutdown() joins it
        self.assertEqual(calls, [("b", "handoff")])
        self.assertNotIn(b._lifecycle_key("handoff"), oracle_plugin._LIFECYCLE_IN_FLIGHT)

    def test_pending_event_of_stopping_provider_dropped(self):
        a, _, _ = _provider()
        gate, entered = threading.Event(), threading.Event()
        calls = []

        def ingest(name, roots):
            calls.append(name)
            entered.set()
            gate.wait(5)

        with tempfile.TemporaryDirectory() as td, \
                patch("pathlib.Path.home", return_value=Path(td)), \
                patch.dict(sys.modules, {"hermes_constants": None}), \
                patch.object(a, "_skill_ingest_once", side_effect=ingest):
            a.on_skill_lifecycle("edited", "drop")
            self.assertTrue(entered.wait(5))
            a.on_skill_lifecycle("edited", "drop")
            a._write_stop.set()
            gate.set()
            for t in list(a._sync_threads):
                t.join(5)
        self.assertEqual(calls, ["drop"])
        self.assertNotIn(a._lifecycle_key("drop"), oracle_plugin._LIFECYCLE_IN_FLIGHT)
        self.assertNotIn(a._lifecycle_key("drop"), oracle_plugin._LIFECYCLE_PENDING)


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
        cur.getarraydmlrowcounts.return_value = [1, 1]
        with patch.object(p, "_embed_many_tagged", return_value=[([0.1], "M"), ([0.2], "M")]):
            p._embed_worker_loop()  # inline: returns once the queue is empty
        cur.executemany.assert_called_once()
        conn.commit.assert_called_once()
        sql, binds = cur.executemany.call_args[0]
        self.assertTrue(cur.executemany.call_args[1].get("arraydmlrowcounts"))
        self.assertIn("embedding IS NULL", sql)
        self.assertIn("DBMS_LOB.COMPARE(content, :4) = 0", sql)
        # short text is bound as a plain string; no temporary LOBs
        self.assertEqual([(b[2], b[3]) for b in binds], [("m1", "text one"), ("m2", "text two")])
        conn.createlob.assert_not_called()
        self.assertEqual(list(p._embed_pending), [])

    def test_long_text_bound_as_temporary_clob(self):
        p, conn, cur = _provider()
        big = "é" * 3000  # 6,000 UTF-8 bytes: over the plain-string limit
        p._embed_pending.extend([("m1", "short"), ("m2", big)])
        conn.createlob.side_effect = lambda typ, text: ("CLOB", text)
        cur.getarraydmlrowcounts.side_effect = [[1], [1]]
        with patch.object(p, "_embed_many_tagged", return_value=[([0.1], "M"), ([0.2], "M")]):
            p._embed_worker_loop()
        self.assertEqual(cur.executemany.call_count, 2)
        small_binds = cur.executemany.call_args_list[0][0][1]
        big_binds = cur.executemany.call_args_list[1][0][1]
        self.assertEqual([b[3] for b in small_binds], ["short"])
        self.assertEqual([b[3] for b in big_binds], [("CLOB", big)])

    def test_unmatched_row_requeued_with_stored_text_then_given_up(self):
        p, conn, cur = _provider()
        p._embed_pending.append(("m1", "queued text"))
        # Every UPDATE matches nothing; the row still has no embedding and the
        # database holds other text (e.g. a charset substitution).
        cur.getarraydmlrowcounts.return_value = [0]
        cur.fetchall.return_value = [("m1", "stored text")]
        texts = []

        def embed(batch):
            texts.extend(batch)
            return [([0.1], "M") for _ in batch]

        with patch.object(p, "_embed_many_tagged", side_effect=embed):
            p._embed_worker_loop()
        self.assertEqual(texts[0], "queued text")
        self.assertEqual(set(texts[1:]), {"stored text"})
        # bounded: gives up after _BACKFILL_MAX_ATTEMPTS re-reads
        self.assertEqual(len(texts), oracle_plugin._BACKFILL_MAX_ATTEMPTS)
        self.assertEqual(list(p._embed_pending), [])
        self.assertNotIn("m1", p._embed_attempts)

    def test_unmatched_row_already_embedded_is_done(self):
        p, conn, cur = _provider()
        p._embed_pending.append(("m1", "old"))
        cur.getarraydmlrowcounts.return_value = [0]
        cur.fetchall.return_value = []  # embedded meanwhile (or deleted)
        with patch.object(p, "_embed_many_tagged", return_value=[([0.1], "M")]) as emb:
            p._embed_worker_loop()
        emb.assert_called_once()
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
            subst = {"<H>": h, "<P>": str(skill.path)}
            cur.fetchall.return_value = [tuple(subst.get(v, v) if isinstance(v, str) else v for v in r) for r in rows]
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
        res, prov = self._sync([("skill:hosta:demo:c0", "demo", "<H>", "<P>", "hosta", 3)], mode="full")
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
