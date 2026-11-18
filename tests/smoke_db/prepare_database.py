#!/usr/bin/env python3
"""Prepare a local Oracle AI Database Free container for the smoke test.

Waits for the database, creates an empty schema user, and loads the tiny
embedding model from make_embedding_model.py into that schema. After it has
run, tests/verify_provider.py can bootstrap the memory table in the empty
schema and exercise every search mode.

Settings (environment):

    SMOKE_DB_ADMIN_PASSWORD   password of the admin user (required)
    SMOKE_DB_ADMIN_USER       default SYSTEM
    OCI_DB_DSN                default 127.0.0.1:1521/FREEPDB1
    OCI_DB_USER               schema user to create (required)
    OCI_DB_PASSWORD           its password (required)
    HERMES_ORACLE_ONNX_MODEL  model name to load, default HERMES_SMOKE_EMBED

The script creates things and never drops a user or a table. It refuses a
database that is not on this machine, and a schema user that already exists
(pass --reuse to load the model into an existing user).

Needs the `oracledb` and `onnx` packages.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_embedding_model  # noqa: E402

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,29}$")
# What the plugin needs in its own schema: a session, the table and its
# indexes, and the in-database embedding model.
_GRANTS = ("CREATE SESSION", "CREATE TABLE", "CREATE MINING MODEL")
_TABLESPACE = "HERMES_SMOKE"


def is_local_dsn(dsn: str) -> bool:
    """True for host:port/service on this machine. Anything else, including
    a TNS alias or a full descriptor, counts as remote."""
    m = re.fullmatch(r"(?:tcps?://)?(\[[0-9a-fA-F:]+\]|[^:/\s()]+)(?::\d+)?/[\w.$#-]+", (dsn or "").strip())
    return bool(m) and m.group(1).lower() in _LOCAL_HOSTS


def wait_for_database(connect, timeout: float, log=print):
    """Connect as the admin user, retrying until the database is open."""
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        try:
            conn = connect()
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM dual")
                cur.fetchone()
            return conn
        except Exception as e:  # noqa: BLE001
            last = (str(e).strip().splitlines() or [type(e).__name__])[0]
            if time.monotonic() >= deadline:
                raise RuntimeError(f"database not reachable after {timeout:.0f}s: {last}") from e
            log(f"waiting for the database ({last[:80]})")
            time.sleep(5)


def data_tablespace(cur, log=print) -> str:
    """A tablespace with automatic segment space management, which VECTOR
    columns need. The "lite" image has no USERS tablespace and defaults to
    SYSTEM, so one is created there."""
    cur.execute(
        "SELECT t.tablespace_name FROM database_properties p JOIN dba_tablespaces t "
        "ON t.tablespace_name = p.property_value "
        "WHERE p.property_name = 'DEFAULT_PERMANENT_TABLESPACE' AND t.segment_space_management = 'AUTO'"
    )
    row = cur.fetchone()
    if row and row[0] != "SYSTEM":
        return row[0]
    cur.execute("SELECT COUNT(*) FROM dba_tablespaces WHERE tablespace_name = :n", [_TABLESPACE])
    if not cur.fetchone()[0]:
        cur.execute("SELECT file_name FROM dba_data_files WHERE tablespace_name = 'SYSTEM' FETCH FIRST 1 ROWS ONLY")
        directory = cur.fetchone()[0].rsplit("/", 1)[0]
        cur.execute(
            f"CREATE TABLESPACE {_TABLESPACE} DATAFILE '{directory}/{_TABLESPACE.lower()}01.dbf' "
            "SIZE 100M AUTOEXTEND ON NEXT 50M SEGMENT SPACE MANAGEMENT AUTO"
        )
        log(f"created tablespace {_TABLESPACE}")
    return _TABLESPACE


def create_schema_user(cur, user: str, password: str, reuse: bool, log=print) -> None:
    cur.execute("SELECT COUNT(*) FROM all_users WHERE username = :u", [user.upper()])
    if cur.fetchone()[0]:
        if not reuse:
            raise RuntimeError(f"user {user} already exists; pass --reuse to keep it and load the model")
        log(f"keeping existing user {user}")
        return
    tablespace = data_tablespace(cur, log)
    quoted = password.replace('"', "")
    cur.execute(
        f'CREATE USER {user} IDENTIFIED BY "{quoted}" DEFAULT TABLESPACE {tablespace} '
        f"QUOTA UNLIMITED ON {tablespace}"
    )
    for grant in _GRANTS:
        cur.execute(f"GRANT {grant} TO {user}")
    log(f"created user {user} in tablespace {tablespace}")


def load_model(conn, model_name: str, data: bytes, log=print) -> int:
    """Load the model into the connected schema, replacing an earlier copy of
    the same name. Returns the dimension count the database reports."""
    import oracledb

    with conn.cursor() as cur:
        try:
            cur.execute("BEGIN DBMS_VECTOR.DROP_ONNX_MODEL(model_name => :n, force => TRUE); END;", n=model_name)
        except oracledb.DatabaseError:
            pass
        cur.setinputsizes(d=oracledb.DB_TYPE_BLOB)
        cur.execute(
            "BEGIN DBMS_VECTOR.LOAD_ONNX_MODEL(model_name => :n, model_data => :d, metadata => JSON(:m)); END;",
            n=model_name, d=data, m=make_embedding_model.LOAD_METADATA,
        )
        cur.execute(f"SELECT VECTOR_EMBEDDING({model_name} USING :t AS data) FROM dual", t="smoke test")  # nosec B608
        dims = len(cur.fetchone()[0])
    log(f"loaded model {model_name} ({len(data)} bytes, {dims} dimensions)")
    return dims


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--reuse", action="store_true", help="keep an existing schema user and only load the model")
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for the database (default 600)")
    args = parser.parse_args(argv)

    dsn = os.environ.get("OCI_DB_DSN") or "127.0.0.1:1521/FREEPDB1"
    admin_user = os.environ.get("SMOKE_DB_ADMIN_USER") or "SYSTEM"
    admin_password = os.environ.get("SMOKE_DB_ADMIN_PASSWORD") or ""
    user = os.environ.get("OCI_DB_USER") or ""
    password = os.environ.get("OCI_DB_PASSWORD") or ""
    model_name = os.environ.get("HERMES_ORACLE_ONNX_MODEL") or "HERMES_SMOKE_EMBED"

    missing = [k for k, v in (("SMOKE_DB_ADMIN_PASSWORD", admin_password), ("OCI_DB_USER", user),
                              ("OCI_DB_PASSWORD", password)) if not v]
    if missing:
        print(f"missing settings: {', '.join(missing)}", file=sys.stderr)
        return 2
    if not is_local_dsn(dsn):
        print("refusing: OCI_DB_DSN must be host:port/service on this machine (127.0.0.1 or localhost); "
              "this script creates a user and is meant for a local test container", file=sys.stderr)
        return 2
    for label, name in (("OCI_DB_USER", user), ("HERMES_ORACLE_ONNX_MODEL", model_name)):
        if not _NAME_RE.fullmatch(name):
            print(f"{label} must be a plain identifier (letters, digits, underscore)", file=sys.stderr)
            return 2

    try:
        import oracledb

        model = make_embedding_model.build_model()
    except ImportError as e:
        print(f"missing package: {e.name}. Install it: python -m pip install oracledb onnx", file=sys.stderr)
        return 2

    admin = wait_for_database(
        lambda: oracledb.connect(user=admin_user, password=admin_password, dsn=dsn), args.timeout
    )
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT banner_full FROM v$version")
            print(cur.fetchone()[0].splitlines()[0])
            create_schema_user(cur, user, password, args.reuse)
    finally:
        admin.close()

    conn = oracledb.connect(user=user, password=password, dsn=dsn)
    try:
        dims = load_model(conn, model_name, model)
    finally:
        conn.close()
    if dims != make_embedding_model.DIMENSIONS:
        print(f"the model returned {dims} dimensions, expected {make_embedding_model.DIMENSIONS}", file=sys.stderr)
        return 1
    print(f"ready: run tests/verify_provider.py with HERMES_ORACLE_ONNX_MODEL={model_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
