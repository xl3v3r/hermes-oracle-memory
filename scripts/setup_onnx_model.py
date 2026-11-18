#!/usr/bin/env python3
"""Export the 768-dimension BGE embedding model as an ONNX pipeline and load it
into Oracle Autonomous AI Database through DATA_PUMP_DIR.

The pipeline (tokenizer, model, pooling, normalisation in one ONNX graph) is
built by onnx2oracle, a separate project by jasperan
(https://github.com/jasperan/onnx2oracle, MIT licence). Install it first:

    python -m pip install onnx2oracle

The OCI_DB_* settings come from the environment, or from --env-file.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

try:  # helpers shared by the scripts (scripts/_common.py)
    from . import _common as common
except ImportError:  # run as a script, or imported with this directory on sys.path
    import _common as common  # type: ignore[no-redef]

load_env = common.load_env

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("setup_onnx")

MODEL_NAME = "BGE_BASE_EN_V1_5"
FILE_NAME = "bge_base_en_v1_5.onnx"
_MISSING_ONNX2ORACLE = (
    "This script needs onnx2oracle (https://github.com/jasperan/onnx2oracle), which is not installed. "
    "Install it: python -m pip install onnx2oracle"
)


def build_model() -> bytes:
    """The ONNX pipeline for BAAI/bge-base-en-v1.5, built with onnx2oracle and
    cached under ~/.cache. Raises ImportError when onnx2oracle is missing."""
    from onnx2oracle.pipeline import build_augmented
    from onnx2oracle.presets import ModelSpec

    spec = ModelSpec(
        hf_repo="BAAI/bge-base-en-v1.5",
        dims=768,
        pooling="cls",
        normalize=True,
        oracle_name=MODEL_NAME,
        max_length=512,
    )
    local_onnx_path = Path.home() / ".cache" / "bge_base_en_v1_5_sliced.onnx"
    if local_onnx_path.exists():
        logger.info("Loading cached ONNX pipeline from %s", local_onnx_path)
        return local_onnx_path.read_bytes()
    logger.info("Building augmented ONNX pipeline for %s...", spec.hf_repo)
    t0 = time.time()
    model_bytes = build_augmented(spec)
    logger.info("Pipeline built in %.2fs. Size: %.2f MB", time.time() - t0, len(model_bytes) / (1024 * 1024))
    local_onnx_path.parent.mkdir(parents=True, exist_ok=True)
    local_onnx_path.write_bytes(model_bytes)
    return model_bytes


def load_model(model_bytes: bytes) -> None:
    import oracledb

    logger.info("Connecting to Oracle Database...")
    conn = oracledb.connect(**common.connect_kwargs())
    cur = conn.cursor()
    for stmt in (
        "ALTER SESSION SET parallel_degree_policy = MANUAL",
        "ALTER SESSION DISABLE PARALLEL QUERY",
        "ALTER SESSION DISABLE PARALLEL DML",
    ):
        try:
            cur.execute(stmt)
        except Exception:
            pass

    # Step 1: Write to DATA_PUMP_DIR
    logger.info("Writing ONNX model to DATA_PUMP_DIR/%s ...", FILE_NAME)
    t0 = time.time()
    cur.execute("""
    DECLARE
        l_blob BLOB := :model_data;
        l_file UTL_FILE.FILE_TYPE;
        l_pos NUMBER := 1;
        l_amount BINARY_INTEGER := 32767;
        l_buffer RAW(32767);
        l_len NUMBER;
    BEGIN
        l_len := DBMS_LOB.GETLENGTH(l_blob);
        l_file := UTL_FILE.FOPEN('DATA_PUMP_DIR', :file_name, 'wb', 32767);
        WHILE l_pos <= l_len LOOP
            IF l_pos + l_amount - 1 > l_len THEN
                l_amount := l_len - l_pos + 1;
            END IF;
            DBMS_LOB.READ(l_blob, l_amount, l_pos, l_buffer);
            UTL_FILE.PUT_RAW(l_file, l_buffer, TRUE);
            l_pos := l_pos + l_amount;
        END LOOP;
        UTL_FILE.FCLOSE(l_file);
    EXCEPTION
        WHEN OTHERS THEN
            IF UTL_FILE.IS_OPEN(l_file) THEN
                UTL_FILE.FCLOSE(l_file);
            END IF;
            RAISE;
    END;
    """, {
        "model_data": model_bytes,
        "file_name": FILE_NAME,
    })
    logger.info("Wrote file to DATA_PUMP_DIR in %.2fs", time.time() - t0)

    # Verify file exists in DATA_PUMP_DIR
    cur.execute("SELECT /*+ NO_PARALLEL */ object_name, bytes FROM DBMS_CLOUD.LIST_FILES('DATA_PUMP_DIR') WHERE object_name = :fn", [FILE_NAME])
    row = cur.fetchone()
    logger.info("File in DATA_PUMP_DIR: %s", row)

    # Step 2: Drop existing model if present
    logger.info("Checking if model %s already exists...", MODEL_NAME)
    cur.execute("SELECT /*+ NO_PARALLEL */ COUNT(*) FROM USER_MINING_MODELS WHERE MODEL_NAME = :m", [MODEL_NAME])
    if cur.fetchone()[0] > 0:
        logger.info("Dropping existing model %s...", MODEL_NAME)
        cur.execute("BEGIN DBMS_VECTOR.DROP_ONNX_MODEL(MODEL_NAME => :m, FORCE => TRUE); END;", [MODEL_NAME])
        conn.commit()

    # Step 3: Load into Oracle via DBMS_VECTOR.LOAD_ONNX_MODEL from DATA_PUMP_DIR
    logger.info("Loading ONNX model %s via DBMS_VECTOR.LOAD_ONNX_MODEL...", MODEL_NAME)
    metadata = json.dumps({
        "function": "embedding",
        "embeddingOutput": "embedding",
        "input": {"pre_text": ["DATA"]},
    })
    t0 = time.time()
    cur.execute("""
    BEGIN
        DBMS_VECTOR.LOAD_ONNX_MODEL(
            DIRECTORY => 'DATA_PUMP_DIR',
            FILE_NAME => :file_name,
            MODEL_NAME => :model_name,
            METADATA   => JSON(:metadata)
        );
    END;
    """, {
        "file_name": FILE_NAME,
        "model_name": MODEL_NAME,
        "metadata": metadata,
    })
    conn.commit()
    logger.info("Model loaded in %.2fs", time.time() - t0)

    # Step 4: Verify in-database inference
    logger.info("Testing in-database VECTOR_EMBEDDING with %s...", MODEL_NAME)
    cur.execute(f"SELECT /*+ NO_PARALLEL */ VECTOR_EMBEDDING({MODEL_NAME} USING 'Oracle 23ai AI Vector Search test' AS DATA) FROM DUAL")
    emb = cur.fetchone()[0]
    logger.info("Result vector type: %s, len: %d", type(emb), len(emb) if emb is not None else 0)
    assert emb is not None, "Failed: VECTOR_EMBEDDING returned None"
    assert len(emb) == 768, f"Failed: Expected 768 dimensions, got {len(emb)}"

    logger.info("SUCCESS! Model %s is loaded and working with 768 dimensions!", MODEL_NAME)
    conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--env-file", default=None, help="load OCI_DB_* settings from this KEY=VALUE file (default: environment only)")
    args = parser.parse_args(argv)
    load_env(args.env_file)
    try:
        model_bytes = build_model()
    except ImportError as e:
        if (e.name or "").split(".")[0] != "onnx2oracle":
            raise
        print(_MISSING_ONNX2ORACLE, file=sys.stderr)
        return 2
    load_model(model_bytes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
