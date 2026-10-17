#!/usr/bin/env python3
"""Re-embed / verify embeddings in the Oracle 26ai memory table using ONLY the
in-database ONNX model. No external provider is ever contacted.

Why this exists (measured on the PHX ADB, 2 OCPU, 2026-10):
  * `scripts/backfill_embeddings.py` only touches rows WHERE embedding IS NULL,
    hard-requires OPENROUTER_API_KEY, and falls back to OpenRouter on ONNX error.
  * ADB runs `parallel_degree_policy=AUTO` (Exadata auto-DOP). A statement that
    scans this table for >~10s gets parallelised even though the table is
    DEGREE 1; the PX slaves then stall forever in `PX Deq Credit: send blkd`
    while the coordinator waits in `PX Deq: Execution Msg`. That is the
    "bulk re-embed hangs" bug. Every session here forces serial execution and
    aborts if a PX slave appears.
  * In-database ONNX inference is ~185 ms/row and is the ONLY significant cost
    (writing vectors runs ~2,700 rows/s). Throughput scales with CONCURRENT
    SESSIONS, not batch size - measured on this shape:
        1 worker  3.4 rows/s   2 workers  4.9 rows/s
        4 workers 8.4 rows/s   8 workers  9.1 rows/s but hits ORA-04036
                                           (PGA_AGGREGATE_LIMIT)
    So 4 is the practical ceiling and the default is 3, leaving headroom for the
    live gateway, which also embeds on write.
  * Batching the inference itself does NOT help: the plugin's UNION ALL binds
    trick measured 6.1 rows/s against 5.3 for a set-based UPDATE.

Modes
  verify   read-only; reports how many rows already match the internal model
  stale    recomputes and rewrites only rows whose stored vector differs (MERGE)
  reembed  blind rewrite of every row in range (use only for a model migration)
  nulls    backfills and stamps rows missing embeddings or model provenance

Each worker owns a disjoint MEMORY_ID range, pages by keyset and commits per
batch, so the job is interruptible and resumable via --checkpoint --resume.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("reembed_embeddings")

_DEFAULT_MODEL = "BGE_BASE_EN_V1_5"
_DEFAULT_TABLE = "HERMES_AGENT_MEMORY"
_DEFAULT_BATCH = 500
_DEFAULT_TOL = 0.01
_DEFAULT_WORKERS = 3
_MAX_CONTENT = 4000  # the plugin embeds (content or "")[:4000]
_PGA_ERR = "ORA-04036"


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

    user = os.environ.get("OCI_DB_USER")
    password = os.environ.get("OCI_DB_PASSWORD")
    dsn = os.environ.get("OCI_DB_DSN")
    if not user or not password or not dsn:
        raise RuntimeError("Missing OCI_DB_USER / OCI_DB_PASSWORD / OCI_DB_DSN")

    wallet_dir = os.environ.get("OCI_WALLET_LOCATION")
    if not wallet_dir:
        for candidate in (Path.home() / ".oci" / "wallet", Path.home() / ".hermes" / "oracle_wallet"):
            if (candidate / "cwallet.sso").exists() or (candidate / "tnsnames.ora").exists():
                wallet_dir = str(candidate)
                break

    kwargs: dict[str, Any] = {"user": user, "password": password, "dsn": dsn}
    if wallet_dir:
        kwargs["config_dir"] = wallet_dir
        kwargs["wallet_location"] = wallet_dir
        wallet_pwd = os.environ.get("OCI_WALLET_PASSWORD")
        if wallet_pwd:
            kwargs["wallet_password"] = wallet_pwd
    return oracledb.connect(**kwargs)


def guard_serial(cur: Any) -> None:
    """Force serial execution. ADB auto-DOP is what hangs VECTOR_EMBEDDING work."""
    for stmt in (
        "ALTER SESSION SET parallel_degree_policy = MANUAL",
        "ALTER SESSION DISABLE PARALLEL QUERY",
        "ALTER SESSION DISABLE PARALLEL DML",
    ):
        try:
            cur.execute(stmt)
        except Exception as e:  # pragma: no cover - privileges vary
            logger.debug("could not run %r: %s", stmt, e)


def assert_no_px(cur: Any) -> None:
    try:
        cur.execute(
            "SELECT COUNT(*) FROM v$px_session "
            "WHERE qcsid = TO_NUMBER(SYS_CONTEXT('USERENV', 'SID')) "
            "  AND sid != TO_NUMBER(SYS_CONTEXT('USERENV', 'SID'))"
        )
        px = cur.fetchone()[0]
    except Exception:
        cur.execute("SELECT COUNT(*) FROM v$px_session")
        px = cur.fetchone()[0]
    if px:
        raise RuntimeError(
            f"{px} PX slave session(s) appeared - parallel execution is active and this "
            "statement is at risk of the PX Deq Credit hang. Aborting before it stalls."
        )


def content_expr(alias: str) -> str:
    # exactly what the plugin embeds: (content or "")[:4000]
    return f"DBMS_LOB.SUBSTR({alias}.content,{_MAX_CONTENT},1)"


def partition_bounds(table: str, workers: int, nulls_only: bool = False) -> list[str | None]:
    """workers+1 MEMORY_ID boundaries splitting the table into equal ranges."""
    if workers <= 1:
        return [None, None]
    conn = connect()
    cur = conn.cursor()
    guard_serial(cur)
    where = "WHERE (embedding IS NULL OR embedding_model IS NULL)" if nulls_only else "WHERE embedding IS NOT NULL"
    cur.execute(
        f"""SELECT MAX(memory_id) FROM (
              SELECT /*+ NO_PARALLEL */ memory_id,
                     NTILE(:n) OVER (ORDER BY memory_id) g
              FROM {table} {where})
            GROUP BY g ORDER BY g""",  # nosec B608
        [workers],
    )
    highs = [r[0] for r in cur.fetchall() if r[0] is not None]
    bounds: list[str | None] = [None, *highs]
    if len(bounds) == 1:
        bounds.append(None)
    cur.close()
    conn.close()
    return bounds


class Counters:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.scanned = 0
        self.matched = 0
        self.mismatched = 0
        self.written = 0
        self.failed = 0

    def add(self, **kw: int) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {
                "scanned": self.scanned, "matched": self.matched,
                "mismatched": self.mismatched, "written": self.written,
                "failed": self.failed,
            }


def run_worker(
    idx: int, lo: str | None, hi: str, args: argparse.Namespace, model: str,
    table: str, counters: Counters, state: dict[str, Any], stop: threading.Event,
) -> None:
    conn = connect()
    cur = conn.cursor()
    guard_serial(cur)

    last = state.get("last_memory_id") if args.resume else None
    if last is None:
        last = lo

    strikes = 0
    try:
        while not stop.is_set():
            if args.mode == "nulls":
                clauses = ["(m.embedding IS NULL OR m.embedding_model IS NULL)", "m.content IS NOT NULL"]
            else:
                clauses = ["m.embedding IS NOT NULL", "m.content IS NOT NULL"]
            binds: list[Any] = []
            if last is not None:
                clauses.append("m.memory_id > :last")
                binds.append(last)
            clauses.append("m.memory_id <= :hi")
            binds.append(hi)
            inner = (
                f"SELECT /*+ NO_PARALLEL(m) */ m.memory_id, m.content FROM {table} m "
                f"WHERE {' AND '.join(clauses)} ORDER BY m.memory_id"
            )
            sql = f"SELECT memory_id, content FROM ({inner}) WHERE ROWNUM <= :n"
            try:
                cur.execute(sql, binds + [args.batch_size])
                rows = cur.fetchall()
                if not rows:
                    break
                batch_lo, batch_hi = rows[0][0], rows[-1][0]

                if args.mode == "verify":
                    cur.execute(
                        f"""SELECT COUNT(*),
                                   SUM(CASE WHEN VECTOR_DISTANCE(m.embedding,
                                         VECTOR_EMBEDDING({model} USING {content_expr('m')} AS DATA),
                                         COSINE) <= :tol THEN 1 ELSE 0 END)
                            FROM {table} m
                            WHERE m.memory_id >= :lo AND m.memory_id <= :hi
                              AND m.embedding IS NOT NULL AND m.content IS NOT NULL""",  # nosec B608
                        [args.tolerance, batch_lo, batch_hi],
                    )
                    n, ok = cur.fetchone()
                    n, ok = n or 0, ok or 0
                    counters.add(scanned=n, matched=ok, mismatched=n - ok)
                elif args.mode == "nulls":
                    cur.execute(
                        f"""UPDATE {table} m
                               SET m.embedding = VECTOR_EMBEDDING({model} USING {content_expr('m')} AS DATA),
                                   m.embedding_model = :model,
                                   m.embedding_updated_at = SYSTIMESTAMP
                             WHERE /*+ NO_PARALLEL(m) */ m.memory_id >= :lo AND m.memory_id <= :hi
                               AND (m.embedding IS NULL OR m.embedding_model IS NULL)
                               AND m.content IS NOT NULL""",  # nosec B608
                        [model, batch_lo, batch_hi],
                    )
                    counters.add(written=cur.rowcount or 0, scanned=len(rows))
                    conn.commit()
                elif args.mode == "reembed":
                    cur.execute(
                        f"""UPDATE {table} m
                               SET m.embedding = VECTOR_EMBEDDING({model} USING {content_expr('m')} AS DATA),
                                   m.embedding_model = :model,
                                   m.embedding_updated_at = SYSTIMESTAMP
                             WHERE /*+ NO_PARALLEL(m) */ m.memory_id >= :lo AND m.memory_id <= :hi
                               AND m.content IS NOT NULL""",  # nosec B608
                        [model, batch_lo, batch_hi],
                    )
                    counters.add(written=cur.rowcount or 0, scanned=len(rows))
                    conn.commit()
                else:  # stale: one ONNX evaluation per row, writes only what differs
                    cur.execute(
                        f"""MERGE INTO {table} t
                            USING (SELECT /*+ NO_PARALLEL */ m.memory_id AS memory_id,
                                          VECTOR_EMBEDDING({model} USING {content_expr('m')} AS DATA) AS newvec,
                                          m.embedding AS oldvec
                                     FROM {table} m
                                    WHERE m.memory_id >= :lo AND m.memory_id <= :hi
                                      AND m.embedding IS NOT NULL AND m.content IS NOT NULL) s
                               ON (t.memory_id = s.memory_id)
                            WHEN MATCHED THEN UPDATE SET t.embedding = s.newvec,
                                                         t.embedding_model = :model,
                                                         t.embedding_updated_at = SYSTIMESTAMP
                                 WHERE VECTOR_DISTANCE(s.oldvec, s.newvec, COSINE) > :tol""",  # nosec B608
                        [batch_lo, batch_hi, model, args.tolerance],
                    )
                    counters.add(written=cur.rowcount or 0, scanned=len(rows))
                    conn.commit()
            except Exception as e:
                # ORA-04036 can strike the WRITE, not just the read. A worker that
                # dies here leaves its whole range unfinished while the run still
                # reports "done" - a silent partial pass. Back off and retry.
                if _PGA_ERR in str(e) and strikes < args.pga_max_retries:
                    strikes += 1
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    wait = min(30 * strikes, 180)
                    logger.warning("worker %d: ORA-04036, backing off %ds (strike %d/%d)",
                                   idx, wait, strikes, args.pga_max_retries)
                    stop.wait(wait)
                    continue
                raise

            strikes = 0
            last = batch_hi
            state["last_memory_id"] = last
            assert_no_px(cur)
    except Exception as e:
        counters.add(failed=1)
        logger.error("worker %d aborted: %s", idx, e)
    finally:
        cur.close()
        conn.close()


def run(args: argparse.Namespace) -> int:
    load_env()
    table = args.table.strip().upper()
    if not re.match(r"^[A-Z0-9_$]+$", table):
        raise ValueError(f"Invalid Oracle table name: {args.table!r}")
    model = re.sub(r"[^A-Za-z0-9_]", "", args.onnx_model or "")
    if not model:
        raise ValueError("Invalid ONNX model name")

    checkpoint = Path(args.checkpoint).expanduser() if args.checkpoint else None
    saved: dict[str, Any] = {}
    if checkpoint and checkpoint.exists():
        try:
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            logger.info("loaded checkpoint %s (mode=%s)", checkpoint, saved.get("mode"))
        except Exception as e:
            logger.warning("ignoring unreadable checkpoint %s: %s", checkpoint, e)

    if args.mode == "nulls":
        if args.batch_size > 25:
            logger.info("mode=nulls: capping batch size from %d to 25 to stay strictly below Auto DOP threshold (~10s)", args.batch_size)
            args.batch_size = 25
        if args.workers != 1:
            logger.info("mode=nulls: enforcing workers=1 for strictly serial execution to prevent Auto-DOP hangs (was %d)", args.workers)
            args.workers = 1
        if args.total_rows == 162_400:
            args.total_rows = 1_228
    bounds = partition_bounds(table, args.workers, nulls_only=(args.mode == "nulls"))
    # The partition is a snapshot; rows inserted while the job runs would sort
    # above the last boundary and be skipped. Extend the final range to a
    # sentinel that sorts above every MEMORY_ID form in use.
    if bounds:
        bounds[-1] = args.hi_sentinel
    logger.info("table=%s mode=%s model=%s workers=%d total_rows=%d",
                table, args.mode, model, args.workers, args.total_rows)

    counters = Counters()
    stop = threading.Event()
    states: list[dict[str, Any]] = []
    threads: list[threading.Thread] = []
    for i in range(len(bounds) - 1):
        st = dict((saved.get("workers") or {}).get(str(i), {}))
        st.setdefault("last_memory_id", None)
        states.append(st)
        threads.append(threading.Thread(
            target=run_worker,
            args=(i, bounds[i], bounds[i + 1], args, model, table, counters, st, stop),
            name=f"reembed-{i}", daemon=True,
        ))

    t_start = time.time()
    for t in threads:
        t.start()

    def write_checkpoint() -> None:
        if not checkpoint:
            return
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(json.dumps(
            {"mode": args.mode,
             "workers": {str(i): st for i, st in enumerate(states)}}), encoding="utf-8")

    while any(t.is_alive() for t in threads):
        time.sleep(30)
        snap = counters.snapshot()
        el = time.time() - t_start
        rate = snap["scanned"] / el if el else 0
        eta = (args.total_rows - snap["scanned"]) / rate / 60 if rate else 0
        logger.info(
            "scanned=%d matched=%d mismatched=%d written=%d failed=%d  %.1f rows/s  eta %.1f min",
            snap["scanned"], snap["matched"], snap["mismatched"], snap["written"],
            snap["failed"], rate, eta,
        )
        write_checkpoint()
        if args.limit and snap["scanned"] >= args.limit:
            logger.info("reached --limit %d", args.limit)
            stop.set()
            break

    stop.set()
    for t in threads:
        t.join(timeout=120)
    write_checkpoint()

    snap = counters.snapshot()
    el = time.time() - t_start
    logger.info("done: %s", snap)
    logger.info("elapsed %.1f min (%.2f h)", el / 60, el / 3600)
    return 1 if snap["failed"] else 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Re-embed/verify Oracle 26ai memory embeddings using only the in-database ONNX model."
    )
    p.add_argument("--table", default=os.environ.get("HERMES_ORACLE_TABLE", _DEFAULT_TABLE))
    p.add_argument("--mode", choices=("verify", "stale", "reembed", "nulls"), default="verify")
    p.add_argument("--onnx-model", default=os.environ.get("HERMES_ORACLE_ONNX_MODEL", _DEFAULT_MODEL))
    p.add_argument("--batch-size", type=int, default=_DEFAULT_BATCH)
    p.add_argument("--workers", type=int, default=_DEFAULT_WORKERS,
                   help="concurrent sessions; 4 is the measured ceiling, 8 hits ORA-04036")
    p.add_argument("--tolerance", type=float, default=_DEFAULT_TOL,
                   help="cosine distance above which a stored vector counts as stale")
    p.add_argument("--limit", type=int, default=None, help="stop after roughly this many rows")
    p.add_argument("--total-rows", type=int, default=162_400, help="for ETA only")
    p.add_argument("--hi-sentinel", default="zzzzzzzzzzzzzzzzzzzz",
                   help="upper bound for the final worker so rows inserted mid-run are covered")
    p.add_argument("--pga-max-retries", type=int, default=5,
                   help="ORA-04036 backoffs before a worker gives up on its range")
    p.add_argument("--checkpoint", default=None, help="path to a resume checkpoint JSON")
    p.add_argument("--resume", action="store_true", help="resume each worker from its checkpoint")
    args = p.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
