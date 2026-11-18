"""Schema bootstrap (#18): the text index is a plain search index on content,
a FAILED index left by 1.1.1-1.1.1.5 is repaired, DDL failures are logged, and
_verify_schema checks the indexes. No database: the cursor is a fake."""

import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401  (stubs agent.* / tools.*)
import __init__ as oracle_plugin  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
T = "HERMES_AGENT_MEMORY"

# (index_name, index_type, index_subtype, ityp_name, domidx_opstatus, column_name)
PK = ("SYS_C001", "NORMAL", None, None, None, "MEMORY_ID")
TXT = (f"{T}_TXT_IDX", "DOMAIN", None, "CONTEXT_V2", "VALID", "CONTENT")
TXT_FAILED = (f"{T}_TXT_IDX", "DOMAIN", None, "CONTEXT_V2", "FAILED", "CONTENT")
JSON_IDX = (f"{T}_JSON_IDX", "DOMAIN", None, "CONTEXT_V2", "VALID", "METADATA")
HNSW = (f"{T}_VEC_IDX", "VECTOR", "INMEMORY_NEIGHBOR_GRAPH_HNSW", None, None, "EMBEDDING")
IVF = (f"{T}_VEC_IDX", "VECTOR", "NEIGHBOR_PARTITIONS_IVF", None, None, "EMBEDDING")


class FakeCursor:
    """Records statements. Raises the error registered for the first matching
    pattern; fetchall() answers the index-state query from *index_rows*."""

    def __init__(self, index_rows=(), errors=None, dict_error=None):
        self.index_rows = list(index_rows)
        self.errors = dict(errors or {})
        self.dict_error = dict_error
        self.statements: list[str] = []
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, binds=None, **kw):
        self._last = sql
        self.statements.append(sql)
        if "USER_INDEXES" in sql.upper() and self.dict_error:
            raise Exception(self.dict_error)
        for pattern, err in self.errors.items():
            if re.search(pattern, sql, re.I | re.S):
                raise Exception(err)

    def fetchone(self):
        return (0,)

    def fetchall(self):
        return list(self.index_rows) if "USER_INDEXES" in self._last.upper() else []

    def ddl(self):
        return [s for s in self.statements if re.match(r"\s*(CREATE|ALTER|DROP)\b", s, re.I)]


def _provider(cur):
    p = oracle_plugin.OracleMemoryProvider()
    p._table = T
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cur
    p._pool = MagicMock()
    p._pool.acquire.return_value = conn
    return p


def _bootstrap(cur):
    p = _provider(cur)
    with patch.dict(os.environ):
        os.environ.pop("HERMES_ORACLE_SCHEMA_POLICY", None)
        p._ensure_schema()
    return p


class _Case(unittest.TestCase):
    def setUp(self):
        oracle_plugin._SCHEMA_WARNINGS_LOGGED.clear()
        self.addCleanup(oracle_plugin._SCHEMA_WARNINGS_LOGGED.clear)


class TestBootstrapDdl(_Case):
    def test_text_index_is_a_plain_search_index_on_content(self):
        cur = FakeCursor(index_rows=[PK, TXT, HNSW])
        _bootstrap(cur)
        text = [s for s in cur.ddl() if "SEARCH INDEX" in s.upper()]
        self.assertEqual(len(text), 1, cur.ddl())
        self.assertRegex(text[0], rf"(?i)CREATE SEARCH INDEX {T}_TXT_IDX ON {T}\s*\(content\)\s*$")
        for s in cur.statements:
            self.assertNotIn("FOR JSON", s.upper(), s)

    def test_no_statement_swallows_its_own_error(self):
        cur = FakeCursor(index_rows=[PK, TXT, HNSW])
        _bootstrap(cur)
        for s in cur.statements:
            self.assertNotIn("WHEN OTHERS", s.upper(), s)

    def test_vector_index_uses_the_canonical_ddl(self):
        cur = FakeCursor(index_rows=[PK, TXT, HNSW])
        _bootstrap(cur)
        want = oracle_plugin.VECTOR_INDEX_DDL.format(index_name=f"{T}_VEC_IDX", table_name=T)
        self.assertIn(want, cur.ddl())

    def test_fresh_table_has_every_column(self):
        cur = FakeCursor(index_rows=[PK, TXT, HNSW])
        _bootstrap(cur)
        create = next(s for s in cur.ddl() if re.match(r"\s*CREATE TABLE", s, re.I))
        for col in ("memory_id", "session_id", "agent_id", "role", "content", "embedding", "created_at",
                    "target", "metadata", "content_txt", "embedding_model", "embedding_updated_at",
                    "turn_index", "chunk_index", "turn_id", "conv_ref"):
            self.assertRegex(create, rf"(?im)^\s*{col}\s", col)
        self.assertIn("VECTOR(768, FLOAT32)", create)

    def test_older_tables_get_the_later_columns(self):
        cur = FakeCursor(index_rows=[PK, TXT, HNSW])
        _bootstrap(cur)
        added = [s for s in cur.ddl() if re.match(r"\s*ALTER TABLE", s, re.I)]
        for col in ("created_at", "target", "metadata", "content_txt", "embedding_model",
                    "embedding_updated_at", "turn_index", "chunk_index", "turn_id", "conv_ref"):
            self.assertTrue(any(re.search(rf"(?i)ADD \({col}\s", s) for s in added), col)

    def test_table_name_is_interpolated_only_after_sanitising(self):
        # initialize() strips everything but [A-Za-z0-9_]; the DDL must use that value.
        cur = FakeCursor(index_rows=[PK, TXT, HNSW])
        p = _provider(cur)
        p._table = "T1"
        p._ensure_schema()
        for s in cur.ddl():
            self.assertRegex(s, r"\bT1(_TXT_IDX|_VEC_IDX)?\b", s)


class TestDdlFailuresAreReported(_Case):
    def test_failure_is_logged_at_warning_level(self):
        cur = FakeCursor(index_rows=[PK, HNSW], errors={"CREATE SEARCH INDEX": "ORA-01031: insufficient privileges"})
        with self.assertLogs(oracle_plugin.logger, level="WARNING") as logs:
            _bootstrap(cur)
        joined = "\n".join(logs.output)
        self.assertIn("ORA-01031", joined)
        self.assertIn("text index", joined.lower())

    def test_vector_index_failure_is_reported_with_its_reason(self):
        cur = FakeCursor(
            index_rows=[PK, TXT],
            errors={"CREATE VECTOR INDEX": "ORA-51962: The vector memory area is out of space for the current container."},
        )
        with self.assertLogs(oracle_plugin.logger, level="WARNING") as logs:
            p = _bootstrap(cur)
        self.assertIn("ORA-51962", "\n".join(logs.output))
        self.assertTrue(any("vector index" in w for w in p.health()["schema_warnings"]))

    def test_already_exists_errors_stay_quiet(self):
        errors = {
            "CREATE TABLE": "ORA-00955: name is already used by an existing object",
            "CREATE SEARCH INDEX": "ORA-29879: cannot create multiple domain indexes on a column list using same indextype",
            "CREATE VECTOR INDEX": "ORA-01408: such column list already indexed",
            "ALTER TABLE": "ORA-01430: column being added already exists in table",
        }
        cur = FakeCursor(index_rows=[PK, TXT, HNSW], errors=errors)
        with self.assertNoLogs(oracle_plugin.logger, level="WARNING"):
            p = _bootstrap(cur)
        self.assertEqual(p.health()["schema_warnings"], [])

    def test_other_indextype_on_the_column_counts_as_existing(self):
        cur = FakeCursor(
            index_rows=[PK, TXT, HNSW],
            errors={"CREATE SEARCH INDEX": "ORA-29880: such column list already indexed using another domain index and indextype"},
        )
        with self.assertNoLogs(oracle_plugin.logger, level="WARNING"):
            _bootstrap(cur)


class TestFailedTextIndexRepair(_Case):
    """1.1.1-1.1.1.5 created the text index FOR JSON on a CLOB. That statement
    fails and leaves a FAILED domain index, and then every INSERT raises ORA-29861."""

    def test_failed_index_is_dropped_before_the_create(self):
        cur = FakeCursor(index_rows=[PK, TXT_FAILED])

        def fetchall():
            # After the repair the dictionary shows a valid index.
            if "USER_INDEXES" not in cur._last.upper():
                return []
            dropped = any(s.upper().startswith("DROP INDEX") for s in cur.statements)
            return [PK, TXT] if dropped else [PK, TXT_FAILED]

        cur.fetchall = fetchall
        with self.assertLogs(oracle_plugin.logger, level="WARNING") as logs:
            _bootstrap(cur)
        ddl = cur.ddl()
        drop = next(i for i, s in enumerate(ddl) if s.upper().startswith("DROP INDEX"))
        create = next(i for i, s in enumerate(ddl) if "CREATE SEARCH INDEX" in s.upper())
        self.assertLess(drop, create)
        self.assertRegex(ddl[drop], rf"(?i)^DROP INDEX {T}_TXT_IDX( FORCE)?$")
        self.assertIn("FAILED", "\n".join(logs.output))

    def test_valid_index_is_never_dropped(self):
        cur = FakeCursor(index_rows=[PK, TXT, JSON_IDX, HNSW])
        _bootstrap(cur)
        self.assertFalse([s for s in cur.ddl() if s.upper().startswith("DROP")])

    def test_failed_index_under_another_name_is_left_alone_and_reported(self):
        other = ("CUSTOM_TEXT", "DOMAIN", None, "CONTEXT", "FAILED", "CONTENT")
        cur = FakeCursor(index_rows=[PK, other, HNSW])
        with self.assertRaisesRegex(RuntimeError, "CUSTOM_TEXT.*FAILED"):
            _bootstrap(cur)
        self.assertFalse([s for s in cur.ddl() if s.upper().startswith("DROP")])

    def test_failed_json_index_on_metadata_is_not_the_text_index(self):
        failed_json = (f"{T}_JSON_IDX", "DOMAIN", None, "CONTEXT_V2", "FAILED", "METADATA")
        cur = FakeCursor(index_rows=[PK, TXT, failed_json, HNSW])
        _bootstrap(cur)
        self.assertFalse([s for s in cur.ddl() if s.upper().startswith("DROP")])

    def test_require_existing_runs_no_ddl_and_reports_the_failed_index(self):
        cur = FakeCursor(index_rows=[PK, TXT_FAILED, HNSW])
        p = _provider(cur)
        with patch.dict(os.environ, {"HERMES_ORACLE_SCHEMA_POLICY": "REQUIRE_EXISTING"}):
            with self.assertRaisesRegex(RuntimeError, "ORA-29861"):
                p._ensure_schema()
        self.assertEqual(cur.ddl(), [])


class TestVerifySchema(_Case):
    def _verify(self, rows, **kw):
        cur = FakeCursor(index_rows=rows, **kw)
        p = _provider(cur)
        p._verify_schema()
        return p, cur

    def test_complete_schema_has_no_warnings(self):
        with self.assertNoLogs(oracle_plugin.logger, level="WARNING"):
            p, _ = self._verify([PK, TXT, JSON_IDX, HNSW])
        self.assertEqual(p.health()["schema_warnings"], [])

    def test_missing_text_index_is_a_warning_with_the_ddl(self):
        with self.assertLogs(oracle_plugin.logger, level="WARNING") as logs:
            p, _ = self._verify([PK, JSON_IDX, HNSW])
        warnings = p.health()["schema_warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertIn(f"CREATE SEARCH INDEX {T}_TXT_IDX ON {T} (content)", warnings[0])
        self.assertIn("text index", "\n".join(logs.output).lower())

    def test_missing_vector_index_is_a_warning(self):
        with self.assertLogs(oracle_plugin.logger, level="WARNING"):
            p, _ = self._verify([PK, TXT])
        self.assertTrue(any("vector index" in w for w in p.health()["schema_warnings"]))

    def test_failed_text_index_raises_with_the_fix(self):
        cur = FakeCursor(index_rows=[PK, TXT_FAILED, HNSW])
        p = _provider(cur)
        with self.assertRaises(RuntimeError) as ctx:
            p._verify_schema()
        msg = str(ctx.exception)
        self.assertIn(f"{T}_TXT_IDX", msg)
        self.assertIn("ORA-29861", msg)
        self.assertIn("DROP INDEX", msg)

    def test_table_not_owned_by_the_user_is_not_judged(self):
        # A synonym to another schema's table has no rows in USER_INDEXES.
        with self.assertNoLogs(oracle_plugin.logger, level="WARNING"):
            p, _ = self._verify([])
        self.assertEqual(p.health()["schema_warnings"], [])

    def test_dictionary_error_does_not_fail_startup(self):
        with self.assertNoLogs(oracle_plugin.logger, level="WARNING"):
            p, _ = self._verify([], dict_error="ORA-00942: table or view does not exist")
        self.assertEqual(p.health()["schema_warnings"], [])

    def test_table_must_still_be_queryable(self):
        cur = FakeCursor(index_rows=[PK, TXT, HNSW], errors={r"SELECT .*COUNT\(\*\)": "ORA-00942: table or view does not exist"})
        p = _provider(cur)
        with self.assertRaisesRegex(Exception, "ORA-00942"):
            p._verify_schema()

    def test_vector_index_kind_is_recorded(self):
        for row, kind in ((HNSW, "HNSW"), (IVF, "IVF")):
            p, _ = self._verify([PK, TXT, row])
            self.assertEqual(p._vector_index_kind, kind)
        p, _ = self._verify([PK, TXT])
        self.assertEqual(p._vector_index_kind, "")

    def test_health_reports_no_warnings_before_initialize(self):
        self.assertEqual(oracle_plugin.OracleMemoryProvider().health()["schema_warnings"], [])

    def test_warning_is_logged_once_per_process_and_kept_in_health(self):
        # Hermes builds one provider per agent and per API session.
        with self.assertLogs(oracle_plugin.logger, level="WARNING"):
            first, _ = self._verify([PK, TXT])
        with self.assertNoLogs(oracle_plugin.logger, level="WARNING"):
            second, _ = self._verify([PK, TXT])
        self.assertEqual(first.health()["schema_warnings"], second.health()["schema_warnings"])
        self.assertEqual(len(second.health()["schema_warnings"]), 1)

    def test_lower_case_table_name_gives_upper_case_statements(self):
        cur = FakeCursor(index_rows=[PK, HNSW])
        p = _provider(cur)
        p._table = T.lower()
        with self.assertLogs(oracle_plugin.logger, level="WARNING"):
            p._verify_schema()
        self.assertIn(f"CREATE SEARCH INDEX {T}_TXT_IDX ON {T} (content)", p.health()["schema_warnings"][0])


class TestCanonicalDdlIsPublished(_Case):
    """INSTALL.md gives the DDL for REQUIRE_EXISTING installs; it must be the
    DDL the bootstrap runs."""

    def _norm(self, s):
        return re.sub(r"\s+", " ", s).strip().lower()

    def test_install_md_lists_the_bootstrap_statements(self):
        doc = self._norm((REPO / "INSTALL.md").read_text(encoding="utf-8"))
        cur = FakeCursor(index_rows=[PK, TXT, HNSW])
        _bootstrap(cur)
        wanted = [s for s in cur.ddl() if not re.match(r"\s*ALTER TABLE", s, re.I)]
        self.assertEqual(len(wanted), 3, wanted)
        for stmt in wanted:
            self.assertIn(self._norm(stmt), doc, stmt)


if __name__ == "__main__":
    unittest.main()
