#!/usr/bin/env python3
"""Export 768-d ONNX pipeline model and load into Oracle ADB via DATA_PUMP_DIR."""

import json
import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("setup_onnx")

# Load ~/.hermes/.env
env_file = Path.home() / ".hermes" / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from onnx2oracle.presets import ModelSpec
from onnx2oracle.pipeline import build_augmented
import oracledb

MODEL_NAME = "BGE_BASE_EN_V1_5"
FILE_NAME = "bge_base_en_v1_5.onnx"

def main():
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
        model_bytes = local_onnx_path.read_bytes()
    else:
        logger.info("Building augmented ONNX pipeline for %s...", spec.hf_repo)
        t0 = time.time()
        model_bytes = build_augmented(spec)
        logger.info("Pipeline built in %.2fs. Size: %.2f MB", time.time() - t0, len(model_bytes) / (1024 * 1024))
        local_onnx_path.parent.mkdir(parents=True, exist_ok=True)
        local_onnx_path.write_bytes(model_bytes)

    logger.info("Connecting to Oracle Database...")
    conn = oracledb.connect(
        user=os.environ["OCI_DB_USER"],
        password=os.environ["OCI_DB_PASSWORD"],
        dsn=os.environ["OCI_DB_DSN"],
    )
    cur = conn.cursor()

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
    cur.execute("SELECT object_name, bytes FROM DBMS_CLOUD.LIST_FILES('DATA_PUMP_DIR') WHERE object_name = :fn", [FILE_NAME])
    row = cur.fetchone()
    logger.info("File in DATA_PUMP_DIR: %s", row)

    # Step 2: Drop existing model if present
    logger.info("Checking if model %s already exists...", MODEL_NAME)
    cur.execute("SELECT COUNT(*) FROM USER_MINING_MODELS WHERE MODEL_NAME = :m", [MODEL_NAME])
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
    cur.execute(f"SELECT VECTOR_EMBEDDING({MODEL_NAME} USING 'Oracle 23ai AI Vector Search test' AS DATA) FROM DUAL")
    emb = cur.fetchone()[0]
    logger.info("Result vector type: %s, len: %d", type(emb), len(emb) if emb is not None else 0)
    assert emb is not None, "Failed: VECTOR_EMBEDDING returned None"
    assert len(emb) == 768, f"Failed: Expected 768 dimensions, got {len(emb)}"

    logger.info("SUCCESS! Model %s is loaded and working with 768 dimensions!", MODEL_NAME)
    conn.close()

if __name__ == "__main__":
    main()
