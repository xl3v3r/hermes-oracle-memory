#!/usr/bin/env python3
"""Vector index maintenance script for Hermes Oracle Memory.

Drops and recreates the HNSW vector index using the canonical VECTOR_INDEX_DDL
and optionally gathers optimizer statistics.

Usage:
    python3 scripts/rebuild_index.py [--table TABLE] [--index INDEX] [--yes] [--no-gather-stats]

The OCI_DB_* settings come from the environment, or from --env-file.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from typing import Any

try:  # helpers shared by the scripts (scripts/_common.py)
    from . import _common as common
except ImportError:  # run as a script, or imported with this directory on sys.path
    import _common as common  # type: ignore[no-redef]

load_env = common.load_env

logger = logging.getLogger("hermes.rebuild_index")

_DEFAULT_TABLE = "HERMES_AGENT_MEMORY"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")


def connect() -> Any:
    import oracledb

    return oracledb.connect(**common.connect_kwargs())


def _identifier(value: str, what: str) -> str:
    name = value.strip().strip('"').upper()
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"invalid {what}: {value!r}")
    return name


def rebuild_vector_index(
    table_name: str | None = None,
    index_name: str | None = None,
    gather_stats: bool = True,
    force: bool = False,
) -> dict[str, str]:
    """Drop the vector index and create it again. *force* is accepted for
    compatibility; the script has no interactive prompt."""
    clean_table = _identifier(table_name or os.environ.get("HERMES_ORACLE_TABLE") or _DEFAULT_TABLE, "table name")
    actual_index = _identifier(index_name, "index name") if index_name else f"{clean_table}_VEC_IDX"
    ddl = common.load_plugin().VECTOR_INDEX_DDL.format(index_name=actual_index, table_name=clean_table)

    logger.info("Target table: %s, Vector index: %s", clean_table, actual_index)

    conn = connect()
    try:
        with conn.cursor() as cur:
            # 1. Drop existing index if it exists
            logger.info("Dropping existing vector index %s (if present)...", actual_index)
            try:
                cur.execute(f"DROP INDEX {actual_index}")
                conn.commit()
                logger.info("Dropped index %s successfully.", actual_index)
            except Exception as e:
                err_str = str(e)
                if "ORA-01418" in err_str:  # specified index does not exist
                    logger.info("Index %s did not exist; proceeding to create.", actual_index)
                else:
                    logger.warning("DROP INDEX %s encountered: %s", actual_index, e)

            # 2. Recreate using canonical VECTOR_INDEX_DDL
            logger.info("Executing DDL: %s", ddl)
            cur.execute(ddl)
            conn.commit()
            logger.info("Created vector index %s successfully.", actual_index)

            # 3. Gather optimizer statistics
            if gather_stats:
                logger.info("Gathering table and index statistics for %s...", clean_table)
                cur.execute(
                    "BEGIN DBMS_STATS.GATHER_TABLE_STATS(ownname => USER, tabname => UPPER(:1), cascade => TRUE); END;",
                    [clean_table],
                )
                conn.commit()
                logger.info("Statistics gathered successfully.")

            # 4. Verify index status
            cur.execute(
                "SELECT index_name, status, index_type FROM USER_INDEXES WHERE index_name = UPPER(:1)",
                [actual_index],
            )
            row = cur.fetchone()
            status = row[1] if row else "UNKNOWN"
            index_type = row[2] if row else "UNKNOWN"
            logger.info("Index %s verification: status=%s, type=%s", actual_index, status, index_type)
    finally:
        conn.close()

    return {
        "table": clean_table,
        "index": actual_index,
        "status": status,
        "index_type": index_type,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild HNSW vector index in Oracle 26ai.")
    parser.add_argument("--table", default=None, help="Oracle memory table name (default from env or HERMES_AGENT_MEMORY)")
    parser.add_argument("--index", default=None, help="Vector index name (default <TABLE>_VEC_IDX)")
    parser.add_argument("--yes", "-y", action="store_true", help="Proceed without interactive confirmation")
    parser.add_argument("--no-gather-stats", action="store_true", help="Skip gathering table statistics after creation")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose log output")
    parser.add_argument("--env-file", default=None, help="load OCI_DB_* settings from this KEY=VALUE file (default: environment only)")
    args = parser.parse_args()
    load_env(args.env_file)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    result = rebuild_vector_index(
        table_name=args.table,
        index_name=args.index,
        gather_stats=not args.no_gather_stats,
        force=args.yes,
    )
    print(f"Index rebuild complete: table={result['table']}, index={result['index']}, status={result['status']}")


if __name__ == "__main__":
    main()
