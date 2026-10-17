#!/usr/bin/env python3
"""Audit that every row in the memory table is on the expected embedding model.

Two checks, both cheap:
  1. Provenance distribution  - one GROUP BY, no inference. Any model other than
     the expected one (or NULLs among rows written after provenance was enabled)
     is a regression.
  2. Random recompute sample  - 50 rows through VECTOR_EMBEDDING, ~10s. Catches
     rows whose stamp is right but whose vector is not (e.g. a corrupt write).

Prints one verdict line plus detail only when something is wrong, so it is safe
to run from cron: silence means healthy.

Exit codes: 0 = clean, 1 = problems found, 2 = could not audit.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("oracle-memory-audit")

_DEFAULT_MODEL = "BGE_BASE_EN_V1_5"
_DEFAULT_TABLE = "HERMES_AGENT_MEMORY"
_DEFAULT_SAMPLE = 50
_DEFAULT_TOL = 0.01


def load_env() -> None:
    env_file = Path.home() / ".hermes" / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def connect() -> Any:
    import oracledb

    wallet_dir = os.environ.get("OCI_WALLET_LOCATION")
    if not wallet_dir:
        for candidate in (Path.home() / ".oci" / "wallet", Path.home() / ".hermes" / "oracle_wallet"):
            if (candidate / "cwallet.sso").exists() or (candidate / "tnsnames.ora").exists():
                wallet_dir = str(candidate)
                break
    kwargs: dict[str, object] = {
        "user": os.environ["OCI_DB_USER"],
        "password": os.environ["OCI_DB_PASSWORD"],
        "dsn": os.environ["OCI_DB_DSN"],
    }
    if wallet_dir:
        kwargs["config_dir"] = wallet_dir
        kwargs["wallet_location"] = wallet_dir
        pwd = os.environ.get("OCI_WALLET_PASSWORD")
        if pwd:
            kwargs["wallet_password"] = pwd
    return oracledb.connect(**kwargs)


def run(args: argparse.Namespace) -> int:
    load_env()
    table = args.table.strip().upper()
    if not table.replace("_", "").isalnum():
        raise ValueError(f"Invalid table name: {args.table!r}")

    conn = connect()
    cur = conn.cursor()
    # never let auto-DOP touch this: the audit must stay serial and cheap
    for stmt in (
        "ALTER SESSION SET parallel_degree_policy = MANUAL",
        "ALTER SESSION DISABLE PARALLEL QUERY",
        "ALTER SESSION DISABLE PARALLEL DML",
    ):
        try:
            cur.execute(stmt)
        except Exception:
            pass

    problems: list[str] = []

    # ---- check 1: provenance distribution (no inference) --------------------
    cur.execute(
        f"SELECT /*+ NO_PARALLEL */ NVL(embedding_model, '(NULL)'), COUNT(*) FROM {table} GROUP BY embedding_model"
    )  # nosec B608
    dist = cur.fetchall()
    total = sum(n for _m, n in dist)
    logger.info("audit: %s rows, models: %s", f"{total:,}",
                ", ".join(f"{m}={n:,}" for m, n in dist))
    if total == 0:
        logger.warning("AUDIT FAIL: table is empty")
        return 2
    for model, n in dist:
        if model != args.expected_model:
            problems.append(
                f"{n:,} rows carry embedding_model={model!r}, expected {args.expected_model!r}"
            )
    # NULLs are expected only for rows written before provenance existed; after
    # the plugin restart every new write should be stamped, so flag a nonzero
    # count of *recently written* unstamped rows.
    cur.execute(
        f"""SELECT /*+ NO_PARALLEL */ COUNT(*) FROM {table}
            WHERE embedding_model IS NULL
              AND created_at > (SELECT /*+ NO_PARALLEL */ MAX(created_at) FROM {table} WHERE embedding_model IS NOT NULL)"""
    )  # nosec B608
    recent_null = cur.fetchone()[0] or 0
    if recent_null:
        problems.append(
            f"{recent_null:,} rows written after provenance began still have NULL "
            "embedding_model - the deployed plugin may not be restarted yet"
        )

    # ---- check 2: random recompute sample ------------------------------------
    cur.execute(
        f"""SELECT /*+ NO_PARALLEL */ memory_id FROM (
                SELECT /*+ NO_PARALLEL */ memory_id FROM {table}
                WHERE embedding IS NOT NULL AND content IS NOT NULL
                ORDER BY DBMS_RANDOM.VALUE)
            WHERE ROWNUM <= {int(args.sample)}"""
    )  # nosec B608
    ids = [r[0] for r in cur.fetchall()]
    mismatches = []
    for mid in ids:
        cur.execute(
            f"""SELECT /*+ NO_PARALLEL */ ROUND(VECTOR_DISTANCE(m.embedding,
                    VECTOR_EMBEDDING({args.expected_model} USING DBMS_LOB.SUBSTR(m.content, 4000, 1) AS DATA),
                    COSINE), 6)
                FROM {table} m WHERE m.memory_id = :mid""",  # nosec B608
            [mid],
        )
        d = cur.fetchone()[0]
        if d is None or d > args.tolerance:
            mismatches.append((mid, d))
    logger.info("audit: recomputed %d rows, %d mismatched", len(ids), len(mismatches))
    if mismatches:
        for mid, d in mismatches[:10]:
            problems.append(f"row {mid}: stored vector does not match {args.expected_model} (distance {d})")

    conn.close()

    if problems:
        logger.error("AUDIT FAILED - %d problem(s):", len(problems))
        for p in problems:
            logger.error("  - %s", p)
        return 1
    logger.info("AUDIT OK: all %s rows consistent on %s", f"{total:,}", args.expected_model)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Audit Oracle 26ai memory embeddings against the expected in-database model."
    )
    p.add_argument("--table", default=os.environ.get("HERMES_ORACLE_TABLE", _DEFAULT_TABLE))
    p.add_argument("--expected-model", default=os.environ.get("HERMES_ORACLE_ONNX_MODEL", _DEFAULT_MODEL))
    p.add_argument("--sample", type=int, default=_DEFAULT_SAMPLE,
                   help="random rows to recompute (default 50)")
    p.add_argument("--tolerance", type=float, default=_DEFAULT_TOL,
                   help="cosine distance above which a row counts as stale")
    p.add_argument("--quiet", action="store_true",
                   help="print only problems (cron-friendly silence when healthy)")
    args = p.parse_args()
    if args.quiet:
        logging.getLogger().setLevel(logging.ERROR)
    sys.exit(run(args))


if __name__ == "__main__":
    main()
