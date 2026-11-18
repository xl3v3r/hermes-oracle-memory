"""The smoke-test database tools (tests/smoke_db): they must refuse a remote
database, never drop anything, and stay importable without `onnx`. No database."""

import io
import os
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401  (stubs agent.* / tools.*)

SMOKE = Path(__file__).resolve().parent / "smoke_db"
sys.path.insert(0, str(SMOKE))

import make_embedding_model as mem  # noqa: E402
import prepare_database as prep  # noqa: E402

ENV = {"SMOKE_DB_ADMIN_PASSWORD": "a", "OCI_DB_USER": "SMOKE", "OCI_DB_PASSWORD": "b"}


def _main(env, argv=()):
    out, err = io.StringIO(), io.StringIO()
    with patch.dict(os.environ, env, clear=True), redirect_stdout(out), redirect_stderr(err):
        rc = prep.main(list(argv))
    return rc, out.getvalue() + err.getvalue()


class TestLocalOnly(unittest.TestCase):
    def test_local_dsns(self):
        for dsn in ("127.0.0.1:1521/FREEPDB1", "localhost:1521/FREEPDB1", "localhost/FREEPDB1",
                    "[::1]:1521/FREEPDB1", "tcp://127.0.0.1:1521/freepdb1"):
            self.assertTrue(prep.is_local_dsn(dsn), dsn)

    def test_everything_else_is_remote(self):
        for dsn in ("", "db.example.com:1521/svc", "10.0.0.5:1521/FREEPDB1", "myalias", "myalias_high",
                    "(DESCRIPTION=(ADDRESS=(PROTOCOL=tcps)(HOST=127.0.0.1)(PORT=1522))(CONNECT_DATA=(SERVICE_NAME=x)))",
                    "127.0.0.1.example.com:1521/svc", "localhost.example.com/svc",
                    "127.0.0.1:1521/svc?wallet_location=/x"):
            self.assertFalse(prep.is_local_dsn(dsn), dsn)

    def test_remote_database_is_refused_before_any_connection(self):
        with patch.dict(sys.modules, {"oracledb": MagicMock()}) as mods:
            rc, text = _main(dict(ENV, OCI_DB_DSN="db.example.com:1521/svc"))
            mods["oracledb"].connect.assert_not_called()
        self.assertEqual(rc, 2)
        self.assertIn("refusing", text)

    def test_missing_settings(self):
        rc, text = _main({})
        self.assertEqual(rc, 2)
        self.assertIn("SMOKE_DB_ADMIN_PASSWORD, OCI_DB_USER, OCI_DB_PASSWORD", text)

    def test_names_must_be_plain_identifiers(self):
        for key, bad in (("OCI_DB_USER", 'x" IDENTIFIED BY y --'), ("OCI_DB_USER", "a b"),
                         ("HERMES_ORACLE_ONNX_MODEL", "m; DROP TABLE t")):
            rc, text = _main(dict(ENV, **{key: bad}))
            self.assertEqual(rc, 2, bad)
            self.assertIn("plain identifier", text)


class TestNeverDrops(unittest.TestCase):
    def test_sources_drop_no_user_table_or_tablespace(self):
        for f in SMOKE.glob("*.py"):
            src = f.read_text(encoding="utf-8").upper()
            for needle in ("DROP USER", "DROP TABLE", "DROP TABLESPACE", "TRUNCATE", "DELETE FROM"):
                self.assertNotIn(needle, src, f"{f.name}: {needle}")

    def test_existing_user_is_refused_without_reuse(self):
        cur = MagicMock()
        cur.fetchone.return_value = (1,)
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            prep.create_schema_user(cur, "SMOKE", "pw", reuse=False, log=lambda *_: None)
        self.assertEqual(cur.execute.call_count, 1)  # the existence check only

    def test_existing_user_is_kept_with_reuse(self):
        cur = MagicMock()
        cur.fetchone.return_value = (1,)
        prep.create_schema_user(cur, "SMOKE", "pw", reuse=True, log=lambda *_: None)
        self.assertEqual(cur.execute.call_count, 1)

    def test_new_user_gets_only_what_the_plugin_needs(self):
        cur = MagicMock()
        cur.fetchone.side_effect = [(0,), ("USERS",)]
        prep.create_schema_user(cur, "SMOKE", "pw", reuse=False, log=lambda *_: None)
        stmts = [c.args[0] for c in cur.execute.call_args_list]
        grants = sorted(re.sub(r"^GRANT (.*) TO SMOKE$", r"\1", s) for s in stmts if s.startswith("GRANT"))
        self.assertEqual(grants, ["CREATE MINING MODEL", "CREATE SESSION", "CREATE TABLE"])
        create = next(s for s in stmts if s.startswith("CREATE USER"))
        self.assertIn("DEFAULT TABLESPACE USERS", create)

    def test_tablespace_is_created_when_the_default_cannot_hold_vectors(self):
        # The "lite" image: default tablespace SYSTEM, no USERS.
        cur = MagicMock()
        cur.fetchone.side_effect = [None, (0,), ("/opt/oracle/oradata/FREE/FREEPDB1/system01.dbf",)]
        self.assertEqual(prep.data_tablespace(cur, log=lambda *_: None), "HERMES_SMOKE")
        create = cur.execute.call_args_list[-1].args[0]
        self.assertIn("/opt/oracle/oradata/FREE/FREEPDB1/hermes_smoke01.dbf", create)
        self.assertIn("SEGMENT SPACE MANAGEMENT AUTO", create)


class TestWaiting(unittest.TestCase):
    def test_retries_until_the_database_answers(self):
        conn = MagicMock()
        attempts = [RuntimeError("ORA-12514: service not registered"), RuntimeError("ORA-01033: init in progress"), conn]

        def connect():
            r = attempts.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch.object(prep.time, "sleep"):
            self.assertIs(prep.wait_for_database(connect, timeout=60, log=lambda *_: None), conn)

    def test_gives_up_with_the_last_error(self):
        with patch.object(prep.time, "sleep"), patch.object(prep.time, "monotonic", side_effect=[0, 100]):
            with self.assertRaisesRegex(RuntimeError, "not reachable after 10s: ORA-12541"):
                prep.wait_for_database(MagicMock(side_effect=RuntimeError("ORA-12541: no listener")), timeout=10,
                                       log=lambda *_: None)


class TestModel(unittest.TestCase):
    def test_vocabulary_has_one_word_per_dimension(self):
        words = mem.vocabulary()
        self.assertEqual(len(words), mem.DIMENSIONS)
        self.assertEqual(len(set(words)), mem.DIMENSIONS)
        self.assertEqual(mem.DIMENSIONS, 768)

    def test_importable_and_helpful_without_onnx(self):
        with patch.dict(sys.modules, {"onnx": None}), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(mem.main(["--out", os.devnull]), 2)
        self.assertIn("pip install onnx", err.getvalue())

    def test_model_builds_and_matches_the_load_metadata(self):
        try:
            import onnx
        except ImportError:
            self.skipTest("onnx is not installed (the database smoke job installs it)")
        model = onnx.load_model_from_string(mem.build_model())
        self.assertEqual([i.name for i in model.graph.input], [mem.INPUT_NAME])
        self.assertEqual([o.name for o in model.graph.output], [mem.OUTPUT_NAME])
        self.assertEqual(model.graph.output[0].type.tensor_type.shape.dim[1].dim_value, 768)
        self.assertIn(f'"embeddingOutput":"{mem.OUTPUT_NAME}"', mem.LOAD_METADATA)
        self.assertIn(f'"{mem.INPUT_NAME}":["DATA"]', mem.LOAD_METADATA)
        self.assertEqual({n.domain for n in model.graph.node}, {""})  # standard operators only


if __name__ == "__main__":
    unittest.main()
