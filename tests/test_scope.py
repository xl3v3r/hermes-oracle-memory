"""Per-user memory scope (#17, #16): HERMES_ORACLE_SCOPE_MODE=off|auto|required.

off: no statement names scope_id. auto/required: the scope is derived once in
initialize() from what Hermes passes (never from tool arguments), every read
is limited to the caller's rows plus the shared skill corpus, every write,
update and delete to the caller's rows. No database: cursors are mocks.
"""

import json
import os
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401  (stubs agent.* / tools.*)
import __init__ as oracle_plugin  # noqa: E402

ALICE = "u:owui:alice"
BOB = "u:owui:bob"
READ = "(scope_id = :scope OR role = 'skill')"
WRITE = "scope_id = :scope"


def _provider(scope=None, key="", rowcount=1):
    p = oracle_plugin.OracleMemoryProvider()
    p._table = "HERMES_AGENT_MEMORY"
    p._onnx_model = "BGE_BASE_EN_V1_5"
    p._session_id = "sess-1"
    p._scope_id = scope
    p._scope_key = key
    conn, cur = MagicMock(), MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    cur.rowcount = rowcount
    cur.fetchall.return_value = []
    cur.fetchone.return_value = (0,)
    p._pool = MagicMock()
    p._pool.acquire.return_value = conn
    return p, conn, cur


def _statements(cur):
    return [(c[0][0], c[0][1] if len(c[0]) > 1 else None) for c in cur.execute.call_args_list]


def _derive(mode, **kwargs):
    return oracle_plugin._derive_scope(mode, kwargs)


class TestDerivation(unittest.TestCase):
    def test_off_has_no_scope(self):
        d = _derive("off", gateway_session_key="owui:alice", platform="api_server")
        self.assertIsNone(d.scope)
        self.assertEqual(d.error, "")

    def test_session_key_wins(self):
        d = _derive("auto", gateway_session_key="owui:alice", platform="api_server", user_id="7")
        self.assertEqual((d.scope, d.key, d.error), (ALICE, "owui:alice", ""))

    def test_platform_and_user_id(self):
        self.assertEqual(_derive("auto", platform="telegram", user_id="42").scope, "u:telegram:42")
        self.assertEqual(_derive("auto", platform="telegram", user_id=42).scope, "u:telegram:42")

    def test_nothing_is_global_in_auto(self):
        self.assertEqual(_derive("auto", platform="cli").scope, "global")
        self.assertEqual(_derive("auto").scope, "global")

    def test_empty_user_part_is_missing(self):
        for key in ("owui:", "  ", "", "owui: ", ":", None, 7):
            d = _derive("auto", gateway_session_key=key, platform="api_server")
            self.assertEqual(d.scope, "global", key)
            self.assertEqual(d.key, "", key)

    def test_client_key_cannot_claim_global(self):
        for key in ("global", "GLOBAL", "u:global", "shared"):
            scope = _derive("auto", gateway_session_key=key).scope
            self.assertNotEqual(scope, "global")
            self.assertTrue(scope.startswith("u:"), scope)
        self.assertNotEqual(_derive("auto", platform="glo", user_id="bal").scope, "global")

    def test_long_key_is_hashed(self):
        a = _derive("auto", gateway_session_key="k" * 400).scope
        b = _derive("auto", gateway_session_key="k" * 399 + "j").scope
        self.assertTrue(a.startswith("u:sha256:"))
        self.assertLessEqual(len(a), 300)
        self.assertNotEqual(a, b)
        self.assertEqual(a, _derive("auto", gateway_session_key="k" * 400).scope)
        # exactly at the limit is kept verbatim
        self.assertEqual(_derive("auto", gateway_session_key="k" * 298).scope, "u:" + "k" * 298)

    def test_required_fails_closed_without_identity(self):
        for platform in ("api_server", "telegram", "webui", "", None):
            d = _derive("required", platform=platform)
            self.assertIsNone(d.scope, platform)
            self.assertIn("HERMES_ORACLE_SCOPE_MODE=required", d.error)

    def test_required_local_platforms_are_the_operator(self):
        for platform in ("cli", "tui", "cron"):
            d = _derive("required", platform=platform)
            self.assertEqual((d.scope, d.error), ("global", ""), platform)

    def test_required_with_key(self):
        self.assertEqual(_derive("required", gateway_session_key="owui:bob", platform="api_server").scope, BOB)

    def test_unknown_mode_fails_closed(self):
        d = _derive("on", gateway_session_key="owui:alice")
        self.assertIsNone(d.scope)
        self.assertIn("HERMES_ORACLE_SCOPE_MODE", d.error)

    def test_mode_is_case_insensitive_and_defaults_to_off(self):
        self.assertEqual(_derive(" AUTO ", gateway_session_key="owui:alice").scope, ALICE)
        self.assertIsNone(_derive("", gateway_session_key="owui:alice").scope)
        self.assertEqual(_derive("", gateway_session_key="owui:alice").error, "")

    def test_fork_inherits_live_parent(self):
        parent, _, _ = _provider(ALICE, "owui:alice")
        d = oracle_plugin._derive_scope(
            "auto", {"platform": "subagent", "agent_context": "subagent", "parent_session_id": "p1"}, parent=parent)
        self.assertEqual((d.scope, d.key, d.inherited), (ALICE, "owui:alice", True))

    def test_fork_without_live_parent_fails_closed(self):
        for mode in ("auto", "required"):
            d = oracle_plugin._derive_scope(
                mode, {"platform": "subagent", "parent_session_id": "p1", "gateway_session_key": "owui:alice"})
            self.assertIsNone(d.scope)
            self.assertIn("parent", d.error)

    def test_fork_in_off_mode_is_unscoped(self):
        d = oracle_plugin._derive_scope("off", {"platform": "subagent", "parent_session_id": "p1"})
        self.assertEqual((d.scope, d.error), (None, ""))


class TestInitialize(unittest.TestCase):
    def _init(self, mode, **kwargs):
        secrets = {"OCI_DB_PASSWORD": "pw", "OCI_DB_DSN": "db_low", "OCI_DB_USER": "MEM"}
        if mode is not None:
            secrets["HERMES_ORACLE_SCOPE_MODE"] = mode
        p = oracle_plugin.OracleMemoryProvider()
        with patch.object(oracle_plugin, "get_secret", side_effect=lambda n, d=None: secrets.get(n, d)), \
                patch.object(p, "_initialize_pool_locked") as pool:
            p.initialize("sess-9", **kwargs)
        return p, pool

    def test_default_is_off(self):
        p, pool = self._init(None, gateway_session_key="owui:alice", platform="api_server")
        self.assertIsNone(p._scope_id)
        pool.assert_called_once()

    def test_auto_binds_scope(self):
        p, pool = self._init("auto", gateway_session_key="owui:alice", platform="api_server")
        self.assertEqual(p._scope_id, ALICE)
        pool.assert_called_once()

    def test_scope_is_not_the_database_user(self):
        p, _ = self._init("auto", platform="api_server")
        self.assertEqual(p._scope_id, "global")
        self.assertNotIn("MEM", str(p._scope_id))

    def test_required_without_key_never_opens_a_pool(self):
        p, pool = self._init("required", platform="api_server")
        pool.assert_not_called()
        self.assertIsNone(p._pool)
        self.assertIn("HERMES_ORACLE_SCOPE_MODE=required", p._init_error)
        out = p.handle_tool_call("oracle_search", {"query": "x"})
        self.assertIn("not initialized", out)
        self.assertEqual(p.prefetch("anything"), "")

    def test_failed_scope_drops_an_earlier_pool(self):
        p = oracle_plugin.OracleMemoryProvider()
        old = MagicMock()
        p._pool, p._initialized = old, True
        secrets = {"OCI_DB_PASSWORD": "pw", "OCI_DB_DSN": "db_low", "HERMES_ORACLE_SCOPE_MODE": "required"}
        with patch.object(oracle_plugin, "get_secret", side_effect=lambda n, d=None: secrets.get(n, d)), \
                patch.object(p, "_initialize_pool_locked") as pool:
            p.initialize("sess-9", platform="api_server")
        pool.assert_not_called()
        self.assertIsNone(p._pool)
        self.assertFalse(p._initialized)

    def test_reinitialize_rebinds_scope(self):
        p, _ = self._init("auto", gateway_session_key="owui:alice")
        secrets = {"OCI_DB_PASSWORD": "pw", "OCI_DB_DSN": "db_low"}
        with patch.object(oracle_plugin, "get_secret", side_effect=lambda n, d=None: secrets.get(n, d)), \
                patch.object(p, "_initialize_pool_locked"):
            p.initialize("sess-9", gateway_session_key="owui:alice")
        self.assertIsNone(p._scope_id)


class TestOffModeSql(unittest.TestCase):
    """off (the default): no statement names the column, so a table without
    it (REQUIRE_EXISTING installs) keeps working."""

    def test_no_statement_names_scope_id(self):
        p, _, cur = _provider(None)
        p._onnx_in_cooldown = MagicMock(return_value=False)
        sqls = [
            p._insert_sql(embed=True, row_columns=True), p._insert_sql(embed=False, row_columns=False),
            p._row_filters("any")[0],
            p._build_text_search_sql("{a}", 5)[0], p._build_vector_search_sql("a", 5)[0],
            p._build_hybrid_search_sql("a", "{a}", 5)[0],
        ]
        p._update_row("m1", "text")
        p._update_row("m1", None, tags=["a"])
        p._get_rows(memory_ids=["m1"])
        p._delete_memory_mirror("memory", "x")
        p.handle_tool_call("oracle_delete", {"memory_id": "m1"})
        p.handle_tool_call("oracle_delete", {"session_id": "s1"})
        sqls += [s for s, _ in _statements(cur)]
        self.assertGreater(len(sqls), 10)
        for s in sqls:
            self.assertNotIn("scope", s.lower(), s)

    def test_binds_have_no_scope(self):
        p, _, _ = _provider(None)
        row = {"memory_id": "m", "role": "fact", "content": "c", "target": "memory"}
        self.assertEqual(len(p._insert_binds(row, embed_chars=100, row_columns=False)), 10)
        self.assertNotIn("scope", p._row_filters("any")[1])

    def test_ids_are_not_namespaced(self):
        p, _, _ = _provider(None)
        self.assertEqual(p._store_id("handoff:x"), "handoff:x")
        self.assertEqual(p._lookup_ids("handoff:x"), ["handoff:x"])

    def test_bootstrap_adds_no_column(self):
        p, _, cur = _provider(None)
        p._schema_policy = ""
        p._verify_schema = MagicMock()
        p._ensure_schema()
        self.assertFalse([s for s, _ in _statements(cur) if "scope" in s.lower()])


def _insert_map(p, row, *, embed, row_columns):
    """column name -> bound value, following the placeholders of the INSERT."""
    sql = p._insert_sql(embed=embed, row_columns=row_columns)
    binds = p._insert_binds(row, embed_chars=100 if embed else None, row_columns=row_columns)
    cols = [c.strip() for c in re.search(r"\((.*?)\) VALUES", sql, re.S).group(1).split(",")]
    values = re.search(r"VALUES \((.*)\)$", sql, re.S).group(1)
    exprs = re.split(r",\s*(?![^()]*\))", values)
    assert len(cols) == len(exprs), (cols, exprs)
    numbers = sorted(int(n) for n in re.findall(r":(\d+)", sql))
    assert numbers == list(range(1, len(binds) + 1)), (numbers, len(binds))
    out = {}
    for col, expr in zip(cols, exprs):
        m = re.search(r":(\d+)", expr)
        out[col] = binds[int(m.group(1)) - 1] if m else expr.strip()
    return out


class TestScopedInsert(unittest.TestCase):
    ROW = {"memory_id": "m1", "session_id": "s1", "role": "fact", "content": "body", "target": "user",
           "metadata": {"kind": "tool"}, "turn_index": 3, "chunk_index": 4, "turn_id": "t", "conv_ref": "r"}

    def test_every_shape_binds_scope_and_role_to_their_own_columns(self):
        for scope in (ALICE, "global"):
            p, _, _ = _provider(scope)
            p._agent_id = "agent-x"
            for embed in (True, False):
                for row_columns in (True, False):
                    got = _insert_map(p, self.ROW, embed=embed, row_columns=row_columns)
                    self.assertEqual(got["scope_id"], scope)
                    self.assertEqual(got["role"], "fact")
                    self.assertEqual(got["memory_id"], "m1")
                    self.assertEqual(got["session_id"], "s1")
                    self.assertEqual(got["agent_id"], "agent-x")
                    self.assertEqual(got["content"], "body")
                    self.assertEqual(got["target"], "user")
                    if row_columns:
                        self.assertEqual((got["turn_index"], got["conv_ref"]), (3, "r"))

    def test_off_shape_is_unchanged(self):
        p, _, _ = _provider(None)
        got = _insert_map(p, self.ROW, embed=True, row_columns=True)
        self.assertNotIn("scope_id", got)
        self.assertEqual((got["role"], got["content"], got["conv_ref"]), ("fact", "body", "r"))


class TestScopedReads(unittest.TestCase):
    def test_row_filters(self):
        p, _, _ = _provider(ALICE)
        sql, binds = p._row_filters("any")
        self.assertIn(READ, sql)
        self.assertEqual(binds["scope"], ALICE)

    def test_the_three_search_builders(self):
        p, _, _ = _provider(ALICE)
        built = [
            p._build_text_search_sql("{a}", 5), p._build_vector_search_sql("a", 5),
            p._build_hybrid_search_sql("a", "{a}", 5),
        ]
        for sql, binds in built:
            self.assertIn(READ, sql)
            self.assertEqual(binds["scope"], ALICE)
        self.assertEqual(built[2][0].count(READ), 2)  # both legs

    def test_global_session_does_not_read_user_rows(self):
        p, _, _ = _provider("global")
        sql, binds = p._row_filters("any")
        self.assertIn(READ, sql)
        self.assertEqual(binds["scope"], "global")

    def test_get_rows(self):
        p, _, cur = _provider(ALICE)
        p._get_rows(tags=["handoff"])
        sql, binds = _statements(cur)[-1]
        self.assertIn(READ, sql)
        self.assertEqual(binds["scope"], ALICE)

    def test_scope_never_comes_from_tool_arguments(self):
        p, _, cur = _provider(ALICE)
        p.handle_tool_call("oracle_get", {"memory_id": "m1", "scope_id": BOB, "scope": BOB})
        p.handle_tool_call("oracle_delete", {"memory_id": "m1", "scope_id": BOB})
        p.handle_tool_call("oracle_update", {"memory_id": "m1", "tags": ["a"], "scope_id": BOB})
        scopes = [b["scope"] for _, b in _statements(cur) if isinstance(b, dict) and "scope" in b]
        self.assertGreaterEqual(len(scopes), 3)
        self.assertEqual(set(scopes), {ALICE})
        self.assertNotIn(BOB, json.dumps([b for _, b in _statements(cur)], default=str))


class TestScopedWrites(unittest.TestCase):
    def _assert_all_scoped(self, cur, verb, n):
        hits = [(s, b) for s, b in _statements(cur) if re.match(rf"\s*{verb}\b", s, re.I)]
        self.assertEqual(len(hits), n, [s for s, _ in _statements(cur)])
        for sql, binds in hits:
            self.assertIn(WRITE, sql)
            self.assertNotIn(READ, sql)
            self.assertEqual(binds["scope"], ALICE)

    def test_update_tags_only(self):
        p, _, cur = _provider(ALICE)
        p._update_row("m1", None, tags=["a"])
        self._assert_all_scoped(cur, "UPDATE", 1)

    def test_update_content(self):
        p, _, cur = _provider(ALICE)
        p._onnx_in_cooldown = MagicMock(return_value=False)
        p._update_row("m1", "text")
        self._assert_all_scoped(cur, "UPDATE", 1)

    def test_update_in_cooldown(self):
        p, _, cur = _provider(ALICE)
        p._onnx_in_cooldown = MagicMock(return_value=True)
        p._queue_embed_backfill = MagicMock()
        p._update_row("m1", "text")
        self._assert_all_scoped(cur, "UPDATE", 1)

    def test_update_null_embedding_fallback(self):
        p, _, cur = _provider(ALICE)
        p._onnx_in_cooldown = MagicMock(return_value=False)
        p._queue_embed_backfill = MagicMock()
        cur.execute.side_effect = [Exception("ORA-54401: too long"), Exception("ORA-54401: too long"), None]
        p._update_row("m1", "text")
        self._assert_all_scoped(cur, "UPDATE", 3)

    def test_delete_by_id(self):
        p, _, cur = _provider(ALICE)
        out = p.handle_tool_call("oracle_delete", {"memory_id": "m1"})
        self.assertIn("deleted", out)
        self._assert_all_scoped(cur, "DELETE", 1)

    def test_delete_of_another_scopes_row_reports_not_found(self):
        p, _, cur = _provider(ALICE, rowcount=0)
        out = p.handle_tool_call("oracle_delete", {"memory_id": "m1"})
        self.assertIn("Memory not found", out)

    def test_purge_session(self):
        p, _, cur = _provider(ALICE)
        cur.fetchone.return_value = (1,)
        p.handle_tool_call("oracle_delete", {"session_id": "s1"})
        stmts = _statements(cur)
        self.assertEqual(len(stmts), 2)
        for sql, binds in stmts:
            self.assertIn(WRITE, sql)
            self.assertEqual(binds["scope"], ALICE)

    def test_memory_mirror_delete(self):
        p, _, cur = _provider(ALICE)
        p._delete_memory_mirror("memory", "old")
        self._assert_all_scoped(cur, "DELETE", 1)


class TestMemoryIdNamespace(unittest.TestCase):
    def test_scoped_ids_differ_per_scope_and_fit_the_column(self):
        a, _, _ = _provider(ALICE)
        b, _, _ = _provider(BOB)
        ia, ib = a._store_id("handoff:x"), b._store_id("handoff:x")
        self.assertNotEqual(ia, ib)
        self.assertTrue(ia.endswith(":handoff:x"))
        self.assertNotEqual(ia, "handoff:x")
        self.assertLessEqual(len(a._store_id("x" * oracle_plugin._scoped_id_max())), 128)
        self.assertEqual(a._show_id(ia), "handoff:x")
        self.assertEqual(a._show_id(ib), ib)  # never strips someone else's prefix

    def test_global_scope_ids_are_plain(self):
        p, _, _ = _provider("global")
        self.assertEqual(p._store_id("handoff:x"), "handoff:x")

    def test_add_with_memory_id_stores_namespaced_and_answers_plain(self):
        p, _, cur = _provider(ALICE, rowcount=0)
        p._queue_embed_backfill = MagicMock()
        p._onnx_in_cooldown = MagicMock(return_value=False)
        out = json.loads(p.handle_tool_call("oracle_add", {"content": "state", "memory_id": "handoff:x"}))
        self.assertEqual(out["memory_id"], "handoff:x")
        inserts = [(s, b) for s, b in _statements(cur) if s.lstrip().upper().startswith("INSERT")]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][1][0], p._store_id("handoff:x"))

    def test_too_long_id_is_refused_in_a_scoped_session(self):
        p, _, cur = _provider(ALICE)
        out = p.handle_tool_call("oracle_add", {"content": "s", "memory_id": "x" * 128})
        self.assertIn("memory_id", out)
        self.assertIn(str(oracle_plugin._scoped_id_max()), out)
        cur.execute.assert_not_called()

    def test_lookups_match_plain_and_namespaced_ids(self):
        p, _, cur = _provider(ALICE)
        p._get_rows(memory_ids=["handoff:x"])
        _, binds = _statements(cur)[-1]
        self.assertEqual({v for k, v in binds.items() if k.startswith("m")},
                         {"handoff:x", p._store_id("handoff:x")})
        p._update_row("handoff:x", None, tags=["a"])
        sql, binds = _statements(cur)[-1]
        self.assertIn("memory_id IN (:id, :nsid)", sql)
        self.assertEqual((binds["id"], binds["nsid"]), ("handoff:x", p._store_id("handoff:x")))

    def test_results_show_plain_ids(self):
        p, _, cur = _provider(ALICE)
        stored = p._store_id("handoff:x")
        cur.fetchall.return_value = [(stored, "s", "a", "fact", "memory", "body", "{}", None, None)]
        with patch.object(oracle_plugin, "_execute_lobs_as_str", lambda c, s, b: c.execute(s, b)):
            out = json.loads(p.handle_tool_call("oracle_get", {"memory_id": "handoff:x"}))
        self.assertEqual(out["results"][0]["memory_id"], "handoff:x")
        self.assertNotIn("missing", out)

    def test_search_payload_shows_plain_ids(self):
        p, _, _ = _provider(ALICE)
        p._search = MagicMock(return_value=[{"memory_id": p._store_id("n1"), "content": "c"}])
        out = json.loads(p.handle_tool_call("oracle_search", {"query": "c"}))
        self.assertEqual(out["results"][0]["memory_id"], "n1")


class TestCallerKey(unittest.TestCase):
    def test_matching_caller_passes(self):
        p, _, _ = _provider(ALICE, "owui:alice")
        p._search = MagicMock(return_value=[])
        out = p.handle_tool_call("oracle_search", {"query": "x"}, caller_gsk="owui:alice")
        self.assertIn("No relevant", out)

    def test_other_caller_is_refused(self):
        p, _, cur = _provider(ALICE, "owui:alice")
        p._search = MagicMock(return_value=[])
        for caller in ("owui:bob", "", "owui:"):
            out = p.handle_tool_call("oracle_search", {"query": "x"}, caller_gsk=caller)
            self.assertIn("error", out)
            self.assertIn("scope", out)
        p._search.assert_not_called()
        cur.execute.assert_not_called()

    def test_keyed_caller_on_a_global_provider_is_refused(self):
        p, _, _ = _provider("global", "")
        p._search = MagicMock(return_value=[])
        self.assertIn("scope", p.handle_tool_call("oracle_search", {"query": "x"}, caller_gsk="owui:bob"))
        p._search.assert_not_called()

    def test_inherited_scope_accepts_a_keyless_fork(self):
        p, _, _ = _provider(ALICE, "owui:alice")
        p._scope_inherited = True
        p._search = MagicMock(return_value=[])
        self.assertIn("No relevant", p.handle_tool_call("oracle_search", {"query": "x"}, caller_gsk=""))
        self.assertIn("scope", p.handle_tool_call("oracle_search", {"query": "x"}, caller_gsk="owui:bob"))

    def test_off_mode_ignores_the_caller_key(self):
        p, _, _ = _provider(None)
        p._search = MagicMock(return_value=[])
        self.assertIn("No relevant", p.handle_tool_call("oracle_search", {"query": "x"}, caller_gsk="owui:bob"))

    def test_without_the_keyword_the_call_proceeds(self):
        p, _, _ = _provider(ALICE, "owui:alice")
        p._search = MagicMock(return_value=[])
        self.assertIn("No relevant", p.handle_tool_call("oracle_search", {"query": "x"}))


class TestScopedSessionSurface(unittest.TestCase):
    def test_schemas_hide_server_side_tools(self):
        p, _, _ = _provider(ALICE)
        schemas = {s["name"]: s for s in p.get_tool_schemas()}
        self.assertNotIn("oracle_sync_skills", schemas)
        self.assertNotIn("file_path", schemas["oracle_chunk"]["parameters"]["properties"])
        self.assertNotIn("file_path", json.dumps(schemas["oracle_chunk"]))
        # the shared list is not edited in place
        full = {s["name"]: s for s in oracle_plugin.TOOL_SCHEMAS}
        self.assertIn("file_path", full["oracle_chunk"]["parameters"]["properties"])

    def test_operator_and_off_keep_every_tool(self):
        for scope in (None, "global"):
            p, _, _ = _provider(scope)
            schemas = {s["name"]: s for s in p.get_tool_schemas()}
            self.assertIn("oracle_sync_skills", schemas)
            self.assertIn("file_path", schemas["oracle_chunk"]["parameters"]["properties"])

    def test_sync_skills_refused(self):
        p, _, _ = _provider(ALICE)
        p.sync_skills = MagicMock()
        self.assertIn("error", p.handle_tool_call("oracle_sync_skills", {}))
        p.sync_skills.assert_not_called()

    def test_chunk_file_path_refused(self):
        p, _, _ = _provider(ALICE)
        p._chunk_and_store = MagicMock(return_value=[])
        for arg in ("file_path", "path"):
            out = p.handle_tool_call("oracle_chunk", {arg: "/etc/passwd"})
            self.assertIn("error", out)
            self.assertIn("file", out)
        p._chunk_and_store.assert_not_called()

    def test_chunk_content_still_works(self):
        p, _, _ = _provider(ALICE)
        p._chunk_and_store = MagicMock(return_value=["a"])
        self.assertIn("chunked", p.handle_tool_call("oracle_chunk", {"content": "some text"}))

    def test_turn_capture_is_skipped(self):
        p, _, _ = _provider(ALICE)
        p._queue_background_insert = MagicMock()
        p.sync_turn("what is the plan for tomorrow morning", "the plan is ...")
        p.on_session_end([{"role": "user", "content": "a long enough message to be kept as notes"}])
        p._queue_background_insert.assert_not_called()

    def test_operator_turn_capture_is_kept(self):
        for scope in (None, "global"):
            p, _, _ = _provider(scope)
            p._queue_background_insert = MagicMock()
            p.sync_turn("what is the plan for tomorrow morning", "the plan is ...")
            self.assertEqual(p._queue_background_insert.call_count, 1, scope)

    def test_skill_lifecycle_is_skipped(self):
        p, _, _ = _provider(ALICE)
        p._queue_background_insert = MagicMock()
        p._on_skill_lifecycle_impl({"action": "used", "skill_name": "x"})
        p._queue_background_insert.assert_not_called()

    def test_background_rows_never_move_to_another_scope(self):
        a, _, _ = _provider(ALICE)
        b, _, _ = _provider(BOB)
        g, _, _ = _provider(None)
        for q in (a, b, g):
            q._write_stop = MagicMock()
            q._write_stop.is_set.return_value = False
        a._pool = None
        with oracle_plugin._LIVE_PROVIDERS_LOCK:
            for q in (b, g):
                oracle_plugin._LIVE_PROVIDERS.add(q)
        try:
            self.assertIsNone(a._live_provider_for("sess-1"))
            a2, _, _ = _provider(ALICE)
            a2._write_stop = b._write_stop
            with oracle_plugin._LIVE_PROVIDERS_LOCK:
                oracle_plugin._LIVE_PROVIDERS.add(a2)
            self.assertIs(a._live_provider_for("sess-1"), a2)
        finally:
            with oracle_plugin._LIVE_PROVIDERS_LOCK:
                for q in (b, g):
                    oracle_plugin._LIVE_PROVIDERS.discard(q)


class TestScopedSchema(unittest.TestCase):
    def test_bootstrap_adds_column_and_index(self):
        p, _, cur = _provider(ALICE)
        p._schema_policy = ""
        p._verify_schema = MagicMock()
        p._ensure_schema()
        ddl = [s for s, _ in _statements(cur)]
        column = [s for s in ddl if "ADD (scope_id" in s]
        self.assertEqual(column, ["ALTER TABLE HERMES_AGENT_MEMORY ADD (scope_id VARCHAR2(300) DEFAULT 'global' NOT NULL)"])
        index = [s for s in ddl if s.startswith("CREATE INDEX")]
        self.assertEqual(index, ["CREATE INDEX HERMES_AGENT_MEMORY_SCOPE_IDX ON HERMES_AGENT_MEMORY (scope_id)"])
        self.assertLess(ddl.index(column[0]), ddl.index(index[0]))
        # every statement is executable SQL, never a bare column definition
        for s in ddl:
            self.assertRegex(s.strip(), r"^(CREATE|ALTER|DROP|SELECT|BEGIN)\b")

    def test_missing_column_is_an_init_error_naming_the_ddl(self):
        p, _, cur = _provider(ALICE)
        p._index_rows = MagicMock(return_value=[])

        def execute(sql, *a, **k):
            if "scope_id" in sql:
                raise Exception('ORA-00904: "SCOPE_ID": invalid identifier')
        cur.execute.side_effect = execute
        with self.assertRaises(RuntimeError) as ctx:
            p._verify_schema()
        msg = str(ctx.exception)
        self.assertIn("ALTER TABLE HERMES_AGENT_MEMORY ADD (scope_id VARCHAR2(300) DEFAULT 'global' NOT NULL)", msg)
        self.assertIn("HERMES_ORACLE_SCOPE_MODE", msg)

    def test_off_mode_does_not_probe_the_column(self):
        p, _, cur = _provider(None)
        p._index_rows = MagicMock(return_value=[])
        p._verify_schema()
        self.assertFalse([s for s, _ in _statements(cur) if "scope" in s.lower()])


class TestHealth(unittest.TestCase):
    def test_health_reports_mode_not_the_key(self):
        p, _, _ = _provider(ALICE, "owui:alice")
        h = p.health()
        self.assertEqual(h.get("scope"), "user")
        self.assertNotIn("alice", json.dumps(h, default=str))
        self.assertEqual(_provider("global")[0].health().get("scope"), "global")
        self.assertEqual(_provider(None)[0].health().get("scope"), "off")


if __name__ == "__main__":
    unittest.main()
