#!/usr/bin/env python3
"""Backfill and migrate embeddings in Oracle 26ai memory table using OpenRouter.

Model: qwen/qwen3-embedding-4b (1536-d float64)
Target Table: HERMES_AGENT_MEMORY (configurable via --table or HERMES_ORACLE_TABLE)
"""

from __future__ import annotations

import argparse
import array
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_embeddings")

_DEFAULT_MODEL = "qwen/qwen3-embedding-4b"
_DEFAULT_DIM = 768
_DEFAULT_BATCH_SIZE = 64
_OPENROUTER_URL = "https://openrouter.ai/api/v1/embeddings"


def load_env() -> None:
    """Load credentials and configurations from ~/.hermes/.env if present."""
    env_file = Path.home() / ".hermes" / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def get_db_connection() -> Any:
    """Connect to Oracle Autonomous Database using configured credentials and wallet."""
    import oracledb

    user = os.environ.get("OCI_DB_USER")
    password = os.environ.get("OCI_DB_PASSWORD")
    dsn = os.environ.get("OCI_DB_DSN")

    if not user or not password or not dsn:
        raise RuntimeError("Missing database credentials: OCI_DB_USER, OCI_DB_PASSWORD, or OCI_DB_DSN")

    wallet_dir = os.environ.get("OCI_WALLET_LOCATION")
    if not wallet_dir:
        for candidate in (
            Path.home() / ".oci" / "wallet",
            Path.home() / ".hermes" / "oracle_wallet",
        ):
            if (candidate / "cwallet.sso").exists() or (candidate / "tnsnames.ora").exists():
                wallet_dir = str(candidate)
                break

    kwargs: dict[str, Any] = {
        "user": user,
        "password": password,
        "dsn": dsn,
    }
    if wallet_dir:
        kwargs["config_dir"] = wallet_dir
        kwargs["wallet_location"] = wallet_dir
        wallet_pwd = os.environ.get("OCI_WALLET_PASSWORD")
        if wallet_pwd:
            kwargs["wallet_password"] = wallet_pwd

    conn = oracledb.connect(**kwargs)
    try:
        with conn.cursor() as cur:
            for stmt in (
                "ALTER SESSION SET parallel_degree_policy = MANUAL",
                "ALTER SESSION DISABLE PARALLEL QUERY",
                "ALTER SESSION DISABLE PARALLEL DML",
            ):
                try:
                    cur.execute(stmt)
                except Exception:
                    pass
    except Exception:
        pass
    return conn


def get_db_pool(min_conns: int = 1, max_conns: int = 8) -> Any:
    """Create a session pool for multithreaded backfilling."""
    import oracledb

    user = os.environ.get("OCI_DB_USER")
    password = os.environ.get("OCI_DB_PASSWORD")
    dsn = os.environ.get("OCI_DB_DSN")

    if not user or not password or not dsn:
        raise RuntimeError("Missing database credentials: OCI_DB_USER, OCI_DB_PASSWORD, or OCI_DB_DSN")

    wallet_dir = os.environ.get("OCI_WALLET_LOCATION")
    if not wallet_dir:
        for candidate in (
            Path.home() / ".oci" / "wallet",
            Path.home() / ".hermes" / "oracle_wallet",
        ):
            if (candidate / "cwallet.sso").exists() or (candidate / "tnsnames.ora").exists():
                wallet_dir = str(candidate)
                break

    def _init_session(conn: Any, tag: Any = None) -> None:
        try:
            with conn.cursor() as cur:
                for stmt in (
                    "ALTER SESSION SET parallel_degree_policy = MANUAL",
                    "ALTER SESSION DISABLE PARALLEL QUERY",
                    "ALTER SESSION DISABLE PARALLEL DML",
                ):
                    try:
                        cur.execute(stmt)
                    except Exception:
                        pass
        except Exception:
            pass

    kwargs: dict[str, Any] = {
        "user": user,
        "password": password,
        "dsn": dsn,
        "min": min_conns,
        "max": max_conns,
        "session_callback": _init_session,
    }
    if wallet_dir:
        kwargs["config_dir"] = wallet_dir
        kwargs["wallet_location"] = wallet_dir
        wallet_pwd = os.environ.get("OCI_WALLET_PASSWORD")
        if wallet_pwd:
            kwargs["wallet_password"] = wallet_pwd

    return oracledb.create_pool(**kwargs)


def embed_batch_openrouter(
    texts: list[str],
    api_key: str,
    model: str = _DEFAULT_MODEL,
    dimensions: int = _DEFAULT_DIM,
    max_retries: int = 5,
) -> list[array.array | None]:
    """Request batch embeddings from OpenRouter with retry logic."""
    out: list[array.array | None] = [None] * len(texts)
    if not texts:
        return out

    valid_indices = [
        i
        for i, t in enumerate(texts)
        if t is not None and isinstance(t, str) and t.strip()
    ]
    if not valid_indices:
        return out

    payload = {
        "model": model,
        "input": [texts[i][:8000] for i in valid_indices],
        "dimensions": dimensions,
    }
    req_data = json.dumps(payload).encode("utf-8")

    backoff = 2.0
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(
                _OPENROUTER_URL,
                data=req_data,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            for item in data.get("data") or []:
                resp_idx = int(item.get("index", 0))
                vec = item.get("embedding") or []
                if 0 <= resp_idx < len(valid_indices) and len(vec) == dimensions:
                    orig_idx = valid_indices[resp_idx]
                    out[orig_idx] = array.array("f", vec)
            return out
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            logger.warning("OpenRouter HTTP %d on attempt %d/%d: %s", e.code, attempt, max_retries, err_body)
            if e.code == 429 or e.code >= 500:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            else:
                raise
        except Exception as e:
            logger.warning("OpenRouter request error on attempt %d/%d: %s", attempt, max_retries, e)
            if attempt == max_retries:
                raise
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    return out



def run_backfill(
    table_name: str,
    batch_size: int,
    limit: int | None,
    dry_run: bool,
    checkpoint_file: Path,
    rebuild_index: bool,
    dimensions: int = _DEFAULT_DIM,
    onnx_model: str | None = None,
    workers: int = 1,
) -> int:
    """Execute the backfill process."""
    load_env()
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not found in environment or ~/.hermes/.env")

    # Sanitize the ONNX model identifier before it is interpolated into SQL in
    # process_batch(). It originates from a CLI flag / environment variable and
    # is otherwise an injection vector (the table name is validated separately).
    clean_onnx = re.sub(r"[^A-Za-z0-9_]", "", onnx_model or "")
    onnx_model = clean_onnx or None

    completed_ids: set[str] = set()
    if checkpoint_file.exists():
        try:
            with open(checkpoint_file, "r", encoding="utf-8") as f:
                completed_ids = set(json.load(f))
            logger.info("Loaded %d previously completed memory IDs from checkpoint", len(completed_ids))
        except Exception as e:
            logger.warning("Failed to load checkpoint %s: %s", checkpoint_file, e)

    import oracledb

    logger.info("Connecting to Oracle Database...")
    conn = get_db_connection()

    def out_type_handler(cursor: Any, metadata: Any) -> Any:
        if metadata.type_code is oracledb.DB_TYPE_CLOB:
            return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)

    cur = conn.cursor()
    cur.outputtypehandler = out_type_handler

    # Validate table name
    clean_table = table_name.strip().upper()
    if not re.match(r"^[A-Z0-9_$]+$", clean_table):
        raise ValueError(f"Invalid Oracle table name: {table_name!r}")

    logger.info("Checking total rows in table %s...", clean_table)
    cur.execute(f"SELECT /*+ NO_PARALLEL */ count(*) FROM {clean_table}")  # nosec B608
    total_rows = cur.fetchone()[0]
    logger.info("Total rows in %s: %d", clean_table, total_rows)

    logger.info("Fetching rows to embed...")
    query = f"SELECT /*+ NO_PARALLEL */ memory_id, content FROM {clean_table} WHERE embedding IS NULL ORDER BY created_at ASC, memory_id ASC"  # nosec B608
    cur.execute(query)

    rows_to_process: list[tuple[str, str]] = []
    for r in cur:
        mid, content = r[0], r[1] or ""
        if mid not in completed_ids:
            rows_to_process.append((mid, content))
            if limit and len(rows_to_process) >= limit:
                break

    cur.close()

    logger.info(
        "Rows remaining to process: %d (already completed: %d)",
        len(rows_to_process),
        len(completed_ids),
    )

    if dry_run:
        sample = rows_to_process[: min(len(rows_to_process), 5)]
        if not sample:
            # Checkpoint had all records; sample 5 directly from table to test connection and embedding
            cur_sample = conn.cursor()
            cur_sample.outputtypehandler = out_type_handler
            cur_sample.execute(f"SELECT /*+ NO_PARALLEL */ memory_id, content FROM {clean_table} WHERE ROWNUM <= 5")  # nosec B608
            sample = [
                (mid, content.read() if hasattr(content, "read") else (content or ""))
                for mid, content in cur_sample.fetchall()
            ]
            cur_sample.close()
        logger.info("[DRY-RUN] Simulating embedding for %d sample items...", len(sample))
        if sample:
            sample_texts = [c for _, c in sample]
            vecs = embed_batch_openrouter(sample_texts, api_key, dimensions=dimensions)
            for (mid, _), vec in zip(sample, vecs):
                logger.info("[DRY-RUN] memory_id=%s embedded successfully: dim=%d", mid, len(vec) if vec else 0)
        logger.info("[DRY-RUN] Finished simulation. No database updates made.")
        conn.close()
        return 0

    processed_count = 0
    t_start = time.time()
    pool = get_db_pool(min_conns=1, max_conns=max(4, workers + 1))

    def process_batch(batch_items: list[tuple[str, str]]) -> tuple[list[str], bool, float]:
        ids = [mid for mid, _ in batch_items]
        texts = [content for _, content in batch_items]
        t0 = time.time()
        if onnx_model:
            try:
                with pool.acquire() as b_conn, b_conn.cursor() as update_cur:
                    sql = f"UPDATE /*+ NO_PARALLEL */ {clean_table} SET embedding = VECTOR_EMBEDDING({onnx_model} USING :1 AS DATA) WHERE memory_id = :2"  # nosec B608
                    binds = [(txt[:4000], mid) for mid, txt in zip(ids, texts)]
                    update_cur.executemany(sql, binds)
                    b_conn.commit()
                return ids, True, time.time() - t0
            except Exception as e:
                logger.warning("In-database ONNX backfill failed for batch (%s); falling back to OpenRouter 768-d", e)

        vecs = embed_batch_openrouter(texts, api_key, dimensions=dimensions)
        binds = [(vec, mid) for mid, vec in zip(ids, vecs) if vec is not None]
        if binds:
            with pool.acquire() as b_conn, b_conn.cursor() as update_cur:
                sql = f"UPDATE /*+ NO_PARALLEL */ {clean_table} SET embedding = :1 WHERE memory_id = :2"  # nosec B608
                update_cur.executemany(sql, binds)
                b_conn.commit()
        success_ids = [mid for mid, vec in zip(ids, vecs) if vec is not None]
        return success_ids, False, time.time() - t0

    batches = [rows_to_process[i : i + batch_size] for i in range(0, len(rows_to_process), batch_size)]
    total_batches = len(batches)

    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_idx = {executor.submit(process_batch, b): idx for idx, b in enumerate(batches)}
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                b_ids, was_onnx, t_embed = future.result()
                for mid in b_ids:
                    completed_ids.add(mid)
                processed_count += len(b_ids)
                elapsed = time.time() - t_start
                rate = processed_count / elapsed if elapsed > 0 else 0
                checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
                with open(checkpoint_file, "w", encoding="utf-8") as f:
                    json.dump(list(completed_ids), f)
                logger.info(
                    "Batch %d/%d (%d rows) done via %s in %.2fs (overall: %d/%d, %.1f rows/sec)",
                    idx + 1,
                    total_batches,
                    len(b_ids),
                    "ONNX" if was_onnx else "OpenRouter",
                    t_embed,
                    processed_count,
                    len(rows_to_process),
                    rate,
                )
    else:
        for idx, batch in enumerate(batches):
            b_ids, was_onnx, t_embed = process_batch(batch)
            for mid in b_ids:
                completed_ids.add(mid)
            processed_count += len(b_ids)
            elapsed = time.time() - t_start
            rate = processed_count / elapsed if elapsed > 0 else 0
            if (idx + 1) % 5 == 0 or processed_count == len(rows_to_process):
                checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
                with open(checkpoint_file, "w", encoding="utf-8") as f:
                    json.dump(list(completed_ids), f)
            logger.info(
                "Batch %d/%d (%d rows) done via %s in %.2fs (overall: %d/%d, %.1f rows/sec)",
                idx + 1,
                total_batches,
                len(b_ids),
                "ONNX" if was_onnx else "OpenRouter",
                t_embed,
                processed_count,
                len(rows_to_process),
                rate,
            )

    pool.close()

    # Maintain vector index / statistics
    if rebuild_index:
        # Check index type in user_indexes
        idx_type = None
        try:
            with conn.cursor() as idx_cur:
                idx_cur.execute(
                    "SELECT /*+ NO_PARALLEL */ index_type FROM user_indexes WHERE table_name = :1 AND index_name = :2",
                    [clean_table, f"{clean_table}_VEC_IDX"],
                )
                row = idx_cur.fetchone()
                if row:
                    idx_type = row[0]
        except Exception:
            pass

        if idx_type is None:
            logger.info("Vector index %s_VEC_IDX not found; creating...", clean_table)
            try:
                with conn.cursor() as idx_cur:
                    idx_cur.execute(
                        f"CREATE VECTOR INDEX {clean_table}_VEC_IDX ON {clean_table} (embedding) ORGANIZATION INMEMORY NEIGHBOR GRAPH"
                    )
                logger.info("Vector index %s_VEC_IDX created successfully.", clean_table)
            except Exception as e:
                logger.warning("Vector index create error: %s", e)
        elif idx_type == "VECTOR":
            logger.info("Index %s_VEC_IDX is of type VECTOR. Vector index maintenance is handled by DBMS_STATS.", clean_table)
        else:
            logger.info("Rebuilding index %s_VEC_IDX...", clean_table)
            try:
                with conn.cursor() as idx_cur:
                    idx_cur.execute(f"ALTER INDEX {clean_table}_VEC_IDX REBUILD")
                logger.info("Index rebuild completed successfully.")
            except Exception as e:
                logger.warning("Index rebuild error: %s", e)

    # Gather table statistics
    logger.info("Gathering statistics on %s...", clean_table)
    try:
        with conn.cursor() as stats_cur:
            stats_cur.execute(
                "BEGIN DBMS_STATS.GATHER_TABLE_STATS(USER, :1); END;",
                [clean_table],
            )
        logger.info("DBMS_STATS.GATHER_TABLE_STATS completed.")
    except Exception as e:
        logger.warning("Gather stats error: %s", e)

    conn.close()
    logger.info("Backfill completed! Total records processed: %d in %.1fs", processed_count, time.time() - t_start)
    return processed_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill embeddings using qwen/qwen3-embedding-4b")
    parser.add_argument(
        "--table",
        default=os.environ.get("HERMES_ORACLE_TABLE", "HERMES_AGENT_MEMORY"),
        help="Target table name (default: HERMES_AGENT_MEMORY)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help="Batch size for embedding requests (default: 64)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of records to process",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run without writing updates to the database",
    )
    parser.add_argument(
        "--checkpoint",
        default="backfills/backfill_progress_768.json",
        help="Path to progress checkpoint file (default: backfills/backfill_progress_768.json)",
    )
    parser.add_argument(
        "--dimensions",
        type=int,
        default=_DEFAULT_DIM,
        help="Embedding dimensions (default: 768)",
    )
    parser.add_argument(
        "--onnx-model",
        default=os.environ.get("HERMES_ORACLE_ONNX_MODEL", "BGE_BASE_EN_V1_5"),
        help="Optional in-database ONNX model name (e.g., BGE_BASE_EN_V1_5)",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        default=True,
        help="Rebuild vector index after backfill",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of concurrent worker threads (default: 4)",
    )
    parser.add_argument(
        "--i-know-this-uses-external-embeddings",
        action="store_true",
        help=(
            "REQUIRED to run. This script embeds via OpenRouter "
            "(qwen/qwen3-embedding-4b), which sends memory content to an "
            "external provider and produces vectors incompatible with the "
            "in-database ONNX model. Use scripts/reembed_embeddings.py "
            "instead - it is ONNX-only, serial-safe, and resumable."
        ),
    )
    args = parser.parse_args()

    if not args.i_know_this_uses_external_embeddings:
        parser.error(
            "refusing to run: this backfill embeds through the EXTERNAL "
            "OpenRouter model (qwen/qwen3-embedding-4b), which sends memory "
            "content outside the database and writes vectors that do not "
            "match the in-database ONNX model, silently splitting the table "
            "across two embedding spaces.\n"
            "For consistency work use:  scripts/reembed_embeddings.py --mode stale\n"
            "For a genuine NULL-embedding backfill that must use the external "
            "model, pass --i-know-this-uses-external-embeddings explicitly."
        )

    checkpoint_path = Path(args.checkpoint).resolve()
    run_backfill(
        table_name=args.table,
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
        checkpoint_file=checkpoint_path,
        rebuild_index=args.rebuild_index,
        dimensions=args.dimensions,
        onnx_model=args.onnx_model,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
