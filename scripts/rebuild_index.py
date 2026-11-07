#!/usr/bin/env python3
"""Vector index maintenance script for Hermes Oracle Memory.

Drops and recreates the HNSW vector index using the canonical VECTOR_INDEX_DDL
and optionally gathers optimizer statistics.

Usage:
    python3 scripts/rebuild_index.py [--table TABLE] [--index INDEX] [--yes] [--no-gather-stats]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Load ~/.hermes/.env
env_file = Path.home() / ".hermes" / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k.strip(), v)

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from __init__ import VECTOR_INDEX_DDL, OracleMemoryProvider

logger = logging.getLogger("hermes.rebuild_index")


def rebuild_vector_index(
    table_name: str | None = None,
    index_name: str | None = None,
    gather_stats: bool = True,
    force: bool = False,
) -> dict[str, str]:
    provider = OracleMemoryProvider()
    if table_name:
        provider._table = table_name
    provider.initialize("rebuild-index")
    if not provider.is_available():
        raise RuntimeError(f"Oracle memory provider unavailable: {provider.unavailable_reason()}")

    actual_table = provider._table or "HERMES_AGENT_MEMORY"
    clean_table = actual_table.upper().strip('"')
    actual_index = index_name.upper().strip('"') if index_name else f"{clean_table}_VEC_IDX"

    logger.info("Target table: %s, Vector index: %s", clean_table, actual_index)

    with provider._pool.acquire() as conn, conn.cursor() as cur:
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
        ddl = VECTOR_INDEX_DDL.format(index_name=actual_index, table_name=clean_table)
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

    provider.shutdown()
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
    args = parser.parse_args()

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
