"""Oracle 26ai memory plugin — MemoryProvider for Autonomous AI Database.

VECTOR IVF + Oracle Text + JSON search as the primary Hermes memory backend.
Credentials: OCI_DB_USER / OCI_DB_PASSWORD / OCI_DB_DSN in $HERMES_HOME/.env.
Embeddings: In-database ONNX model (768-d BGE_BASE_EN_V1_5 via VECTOR_EMBEDDING) strictly
in-database. Keeping all data 100% in-house with zero external third-party endpoint fallback.
"""

from __future__ import annotations

import array
import html
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt
from agent.secret_scope import get_secret
from tools.registry import tool_error

logger = logging.getLogger(__name__)

__version__ = "1.0.9.1"

_PREFETCH_WAIT_SECS = 0.5
_EMBED_DIM = 768
_EMBED_MODEL = "qwen/qwen3-embedding-4b"
_OCI_EMBED_MODEL = "cohere.embed-v4.0"
_EMBED_CACHE_MAX = 256
_EMBED_BATCH = 32
_OCI_RETRY_AFTER_SECS = 300.0
_ONNX_RETRY_AFTER_SECS = 300.0
_FETCH_ARRAYSIZE = 64
_EMBED_TIMEOUT_SECS = 10.0
_EMBED_MAX_RETRIES = 2
_RERANK_MAX_DOCS = 8
_RERANK_ENABLED = (
    os.environ.get("HERMES_ORACLE_DISABLE_RERANK", "").strip().lower()
    not in ("1", "true", "yes")
)
_EXTERNAL_EMBED_KILLSWITCH = (
    os.environ.get("HERMES_ORACLE_DISABLE_EXTERNAL_EMBED", "").strip().lower()
    in ("1", "true", "yes")
)
_MAX_CHUNK_FILE_BYTES = 64 * 1024 * 1024
_CONTAINS_CLEAN_RE = re.compile(r"[^\w\s]")
_INTERNAL_GATEWAY_TURN_RE = re.compile(
    r"^\s*(?:"
    r"\[ASYNC (?:DELEGATION )?(?:BATCH )?COMPLETE[^\]]*\]|"
    r"\[CONTEXT COMPACTION[^\]]*\]|"
    r"\[CONTEXT SUMMARY\]:?|"
    r"\[PRIOR CONTEXT[^\]]*\]|"
    r"\[Your active task list was preserved across context compression\]|"
    r"\[IMPORTANT: Background process \d+ matched watch pattern[^\n]*|"
    r"A background fan-out of \d+ subagent\(s\) you dispatched earlier has finished\.|"
    r"A background subagent you dispatched earlier has finished\."
    r")",
    re.IGNORECASE,
)

TOOL_SCHEMAS = [
    {
        "name": "oracle_search",
        "description": (
            "Primary memory search. Search Oracle 26ai persistent memory (VECTOR cosine + Oracle Text + JSON). "
            "ALWAYS consult oracle_search FIRST before answering questions that depend on prior facts, "
            "preferences, decisions, people, or projects, before falling back to local memory. "
            "mode=hybrid (default) ranks both vector and keyword; vector = semantic only; text = CONTAINS only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "top_k": {
                    "type": "integer",
                    "description": "Max results (default 8, max 30).",
                },
                "mode": {
                    "type": "string",
                    "enum": ["hybrid", "vector", "text"],
                    "description": "Retrieval mode (default hybrid).",
                },
                "target": {
                    "type": "string",
                    "enum": ["memory", "user", "any"],
                    "description": "Restrict to built-in memory vs user profile (default any).",
                },
                "session_id": {
                    "type": "string",
                    "description": "Restrict search to a specific session ID.",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Restrict search to a specific agent ID.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "oracle_add",
        "description": (
            "Primary sink for persistent facts in Oracle 26ai (unlimited storage, embedded + Text + JSON indexed). "
            "Call oracle_add as the primary store whenever storing durable facts, preferences, corrections, "
            "decisions, environment details, or batch knowledge. Do NOT use local 1600-char memory for facts "
            "that belong in Oracle. Accepts a single fact string, list of facts, or 'facts' array."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The fact (or facts) to store. Can be a single declarative fact or multiple facts.",
                },
                "facts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of facts to store in batch.",
                },
                "target": {
                    "type": "string",
                    "enum": ["memory", "user"],
                    "description": "memory = environment/conventions/projects; user = who they are/preferences.",
                },
            },
        },
    },
    {
        "name": "oracle_update",
        "description": "Replace the text of an existing Oracle memory by memory_id (e.g. chk_* or fact_*) from oracle_search. Never use the local memory tool to edit Oracle entries.",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "content": {"type": "string", "description": "New text."},
            },
            "required": ["memory_id", "content"],
        },
    },
    {
        "name": "oracle_delete",
        "description": "Delete an Oracle memory by memory_id (e.g. chk_* or fact_*) or purge an entire session thread by session_id from oracle_search. Never use the local memory tool to remove Oracle entries.",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Specific memory ID to delete."},
                "session_id": {"type": "string", "description": "Session ID to delete all associated memories for thread purge."},
            },
        },
    },
    {
        "name": "oracle_chunk",
        "description": (
            "Split long text or files with in-database DBMS_VECTOR.UTL_TO_CHUNKS, then store "
            "each chunk as its own VECTOR row. Use for documents, logs, notes, or conversation "
            "imports too large for a single fact. Provide 'content' or 'file_path'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Source text to chunk and store.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Path to a text or JSON file on disk to chunk and store.",
                },
                "max_words": {
                    "type": "integer",
                    "description": "Chunk size in words (default 80).",
                },
                "overlap": {
                    "type": "integer",
                    "description": "Word overlap (default 10).",
                },
            },
        },
    },
]

_PROMPT_BODY = (
    "# Oracle 26ai Memory Rules (Hermes Agent)\n\n"
    "Oracle 26ai ADB is the system of record for all lasting memory. There is no local memory store. "
    "If Oracle is unreachable, the agent remembers nothing.\n\n"
    "1. ORACLE-FIRST SEARCH: Before answering any context-dependent question, call oracle_search. "
    "Use results as authoritative context. Re-search with broader terms if ambiguous. "
    "If Oracle is unreachable, state that no memory is available. Ask the user only if Oracle returns nothing.\n\n"
    "2. ORACLE-FIRST FACT SINK: All lasting facts, preferences, decisions, configurations, and learnings "
    "go to Oracle via oracle_add. Use a stable memory_id, compact content (<~200 chars), and tags. "
    "No local fallback — if Oracle is unreachable, facts are not stored.\n\n"
    "3. MEMORY_ID LIFECYCLE: oracle_search before oracle_add to avoid duplicates. "
    "If memory_id exists, use oracle_update(memory_id, ...). Use oracle_delete(memory_id) for obsolete or incorrect facts.\n\n"
    "4. STORAGE TIERS: oracle_add for atomic facts/batch lists; oracle_chunk for documents/logs/conversation exports; "
    "skill_manage for task-specific procedures; no TODO state stored anywhere.\n\n"
    "5. ORACLE-ONLY STORE: There is no local memory. MEMORY.md and all local files are off-limits for facts. "
    "All lasting facts live exclusively in Oracle 26ai.\n\n"
    "6. ORACLE UNAVAILABILITY: If Oracle is unreachable, the agent remembers nothing. No facts are buffered, cached, "
    "or written locally. State clearly that Oracle is unavailable. On reconnection, resume normal operations.\n\n"
    "7. INTERNAL WRITE-AHEAD: When Oracle is reachable, stage writes inside Oracle before committing. "
    "oracle_add to a staging memory_id prefixed with wal_ (e.g., wal_20261017_user_lang) with tags [\"pending\", \"wal\"]. "
    "Validate via oracle_search, then oracle_add the final entry with its canonical memory_id and production tags, "
    "and oracle_delete the wal_ staging entry.\n\n"
    "8. SESSION SCOPING: Tag every Oracle entry: [\"global\"], [\"user:<id>\"], [\"session:<id>\"], "
    "[\"project:<name>\"], [\"session_learn\"], or [\"pending\", \"wal\"] for staging.\n\n"
    "9. VALIDATION BEFORE WRITE: Confirm durability, dedupe via search, keep content compact, normalize terminology."
)


def _is_internal_gateway_turn(text: str) -> bool:
    if not isinstance(text, str):
        return False
    return bool(_INTERNAL_GATEWAY_TURN_RE.match(text or ""))


def _extract_turn_text(msg: Any) -> str:
    """Extract clean string text from a message payload (string, dict, or structured content blocks)."""
    if isinstance(msg, str):
        return msg.strip()
    if not isinstance(msg, dict):
        return ""
    text = msg.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    val = msg.get("value")
    if isinstance(val, str) and val.strip():
        return val.strip()
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                    parts.append(block["text"].strip())
                elif "text" in block and isinstance(block["text"], str) and block["text"].strip():
                    parts.append(block["text"].strip())
            elif isinstance(block, str) and block.strip():
                parts.append(block.strip())
        if parts:
            return "\n\n".join(parts)
    return ""


class OracleMemoryProvider(MemoryProvider):
    """Oracle Autonomous AI Database 26ai VECTOR + Text + JSON memory."""

    def __init__(self) -> None:
        self._pool = None
        self._session_id = ""
        self._agent_id = "hermes"
        self._user = ""
        self._dsn = ""
        self._prefetch_query = ""
        self._prefetch_result = ""
        self._prefetch_done = False
        self._prefetch_count = 0
        self._prefetch_thread: threading.Thread | None = None
        self._embed_lock = threading.Lock()
        self._embed_cache: OrderedDict[str, tuple[array.array, str | None]] = OrderedDict()
        self._embed_cache_lock = threading.Lock()
        self._embed_pending: deque[tuple[str, str]] = deque()
        self._embed_worker: threading.Thread | None = None
        self._oci_embed_failed_at: float | None = None
        self._oci_client: Any | None = None
        self._oci_signer: Any | None = None
        self._onnx_failed_at: float | None = None
        self._lock = threading.Lock()
        self._write_queue: queue.Queue = queue.Queue()
        self._write_worker: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._write_stop = threading.Event()
        self._external_embed_warned = False
        self._init_error = ""
        self._agent_context = "primary"

    @property
    def name(self) -> str:
        return "oracle"

    def is_available(self) -> bool:
        try:
            import oracledb  # noqa: F401
        except Exception:
            return False
        user = get_secret("OCI_DB_USER")
        pwd = get_secret("OCI_DB_PASSWORD")
        dsn = get_secret("OCI_DB_DSN")
        return bool(
            isinstance(user, str)
            and user
            and isinstance(pwd, str)
            and pwd
            and isinstance(dsn, str)
            and dsn
        )

    def unavailable_reason(self) -> str:
        try:
            import oracledb  # noqa: F401
        except Exception:
            return "oracledb is not installed in the Hermes venv"
        missing = [
            k
            for k in ("OCI_DB_USER", "OCI_DB_PASSWORD", "OCI_DB_DSN")
            if not isinstance(get_secret(k), str) or not get_secret(k)
        ]
        if missing:
            return f"missing env: {', '.join(missing)}"
        return ""

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "user",
                "description": "ADB username",
                "secret": True,
                "required": True,
                "env_var": "OCI_DB_USER",
            },
            {
                "key": "password",
                "description": "ADB password",
                "secret": True,
                "required": True,
                "env_var": "OCI_DB_PASSWORD",
            },
            {
                "key": "dsn",
                "description": "ADB TLS connect string",
                "secret": True,
                "required": True,
                "env_var": "OCI_DB_DSN",
            },
            {
                "key": "table_name",
                "description": "Custom table name (for OpenClaw compatibility)",
                "secret": False,
                "required": False,
                "env_var": "HERMES_ORACLE_TABLE",
                "default": "hermes_agent_memory",
            },
            {
                "key": "onnx_model_name",
                "description": "Name of the ONNX model loaded in ADB (for in-database embeddings)",
                "secret": False,
                "required": False,
                "env_var": "HERMES_ORACLE_ONNX_MODEL",
            },
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id if isinstance(session_id, str) else ""
        agent_id = kwargs.get("agent_identity")
        self._agent_id = agent_id if isinstance(agent_id, str) and agent_id else "hermes"
        agent_ctx = kwargs.get("agent_context")
        self._agent_context = agent_ctx if isinstance(agent_ctx, str) and agent_ctx else "primary"

        # Load table name dynamically for OpenClaw shared memory compatibility
        import re as _re

        raw_table = get_secret("HERMES_ORACLE_TABLE")
        table_str = raw_table if isinstance(raw_table, str) and raw_table else "hermes_agent_memory"
        self._table = _re.sub(r"[^a-zA-Z0-9_]", "", table_str) or "hermes_agent_memory"

        raw_onnx = get_secret("HERMES_ORACLE_ONNX_MODEL", "BGE_BASE_EN_V1_5")
        onnx_str = raw_onnx if isinstance(raw_onnx, str) and raw_onnx else "BGE_BASE_EN_V1_5"
        self._onnx_model = _re.sub(r"[^a-zA-Z0-9_]", "", onnx_str)

        user_val = get_secret("OCI_DB_USER", "ADMIN")
        self._user = user_val if isinstance(user_val, str) and user_val else "ADMIN"
        pwd_val = get_secret("OCI_DB_PASSWORD")
        password = pwd_val if isinstance(pwd_val, str) else ""
        dsn_val = get_secret("OCI_DB_DSN")
        self._dsn = dsn_val if isinstance(dsn_val, str) else ""
        if self._dsn:
            # Auto-normalize _high service tier to _low for conservative pool and serial query safety
            self._dsn = _re.sub(r"([a-zA-Z0-9_]+)_high\b", r"\1_low", self._dsn, flags=_re.IGNORECASE)
            if "_high.adb.oraclecloud.com" in self._dsn.lower():
                self._dsn = _re.sub(r"_high\.adb\.oraclecloud\.com", "_low.adb.oraclecloud.com", self._dsn, flags=_re.IGNORECASE)
        if not password or not self._dsn:
            self._init_error = "OCI_DB_PASSWORD or OCI_DB_DSN missing"
            logger.error("Oracle memory: %s", self._init_error)
            return
        try:
            import oracledb

            pool_kwargs: dict[str, Any] = {
                "user": self._user,
                "password": password,
                "dsn": self._dsn,
                "min": 2,
                "max": 6,
                "increment": 1,
                "threaded": True,
                "getmode": oracledb.POOL_GETMODE_WAIT,
                "timeout": 5,
                "wait_timeout": 5,
                "call_timeout": 30000,
            }

            raw_wallet = get_secret("OCI_WALLET_LOCATION")
            wallet_dir = (
                raw_wallet
                if isinstance(raw_wallet, str) and raw_wallet
                else (os.environ.get("TNS_ADMIN") or "")
            )
            if not wallet_dir:
                for candidate in (
                    Path.home() / ".oci" / "wallet",
                    Path.home() / ".hermes" / "oracle_wallet",
                ):
                    try:
                        if (candidate / "cwallet.sso").exists() or (candidate / "tnsnames.ora").exists():
                            wallet_dir = str(candidate)
                            break
                    except Exception:
                        pass

            if wallet_dir and isinstance(wallet_dir, str):
                try:
                    wpath = Path(wallet_dir)
                    if wpath.is_dir():
                        os.environ.setdefault("TNS_ADMIN", str(wallet_dir))
                        pool_kwargs["config_dir"] = str(wallet_dir)
                        pool_kwargs["wallet_location"] = str(wallet_dir)
                        wallet_pwd = get_secret("OCI_WALLET_PASSWORD")
                        if isinstance(wallet_pwd, str) and wallet_pwd:
                            pool_kwargs["wallet_password"] = wallet_pwd
                except Exception:
                    pass

            def _session_callback(conn: Any, tag: Any = None) -> None:
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

            pool_kwargs["session_callback"] = _session_callback

            self._pool = oracledb.SessionPool(**pool_kwargs)
            self._ensure_schema()
            logger.info("Oracle 26ai memory pool ready (session=%s)", self._session_id)
        except Exception as e:
            err_msg = str(e)
            if password and password in err_msg:
                err_msg = err_msg.replace(password, "******")
            self._init_error = err_msg
            logger.error("Oracle memory pool failed: %s", err_msg)
            self._pool = None

    def _ensure_schema(self) -> None:
        if not self._pool:
            return
        policy = (os.environ.get("HERMES_ORACLE_SCHEMA_POLICY") or "").strip().upper()
        if policy == "REQUIRE_EXISTING":
            logger.info("HERMES_ORACLE_SCHEMA_POLICY=REQUIRE_EXISTING set: skipping runtime DDL and verifying schema")
            self._verify_schema()
            return
        # Use DBMS_ASSERT to safely quote the table name and EXECUTE IMMEDIATE to run DDL.
        # This prevents SQL injection vulnerabilities when executing dynamically built schema statements.
        stmts = [
            """
            BEGIN
                EXECUTE IMMEDIATE 'CREATE TABLE ' || DBMS_ASSERT.ENQUOTE_NAME(UPPER(:table_name)) || ' (
                    memory_id VARCHAR2(128) PRIMARY KEY,
                    session_id VARCHAR2(128),
                    agent_id VARCHAR2(64),
                    role VARCHAR2(64),
                    content CLOB,
                    embedding VECTOR(768, FLOAT32),
                    created_at TIMESTAMP DEFAULT SYSTIMESTAMP,
                    target VARCHAR2(32) DEFAULT ''memory'',
                    metadata JSON,
                    content_txt VARCHAR2(4000)
                )';
            END;
            """,
            """
            BEGIN
                EXECUTE IMMEDIATE 'CREATE SEARCH INDEX ' || DBMS_ASSERT.ENQUOTE_NAME(UPPER(:table_name) || '_TXT_IDX') || ' ON ' || DBMS_ASSERT.ENQUOTE_NAME(UPPER(:table_name)) || '(content) FOR JSON';
            END;
            """,
            "BEGIN EXECUTE IMMEDIATE 'CREATE VECTOR INDEX ' || DBMS_ASSERT.ENQUOTE_NAME(UPPER(:table_name) || '_VEC_IDX') || ' ON ' || DBMS_ASSERT.ENQUOTE_NAME(UPPER(:table_name)) || '(embedding) ORGANIZATION INMEMORY NEIGHBOR GRAPH'; EXCEPTION WHEN OTHERS THEN NULL; END;",
            "BEGIN EXECUTE IMMEDIATE 'ALTER TABLE ' || DBMS_ASSERT.ENQUOTE_NAME(UPPER(:table_name)) || ' ADD (created_at TIMESTAMP DEFAULT SYSTIMESTAMP)'; END;",
            "BEGIN EXECUTE IMMEDIATE 'ALTER TABLE ' || DBMS_ASSERT.ENQUOTE_NAME(UPPER(:table_name)) || ' ADD (target VARCHAR2(32) DEFAULT ''memory'')'; END;",
            "BEGIN EXECUTE IMMEDIATE 'ALTER TABLE ' || DBMS_ASSERT.ENQUOTE_NAME(UPPER(:table_name)) || ' ADD (metadata JSON)'; END;",
            "BEGIN EXECUTE IMMEDIATE 'ALTER TABLE ' || DBMS_ASSERT.ENQUOTE_NAME(UPPER(:table_name)) || ' ADD (content_txt VARCHAR2(4000))'; END;",
            "BEGIN EXECUTE IMMEDIATE 'ALTER TABLE ' || DBMS_ASSERT.ENQUOTE_NAME(UPPER(:table_name)) || ' ADD (embedding_model VARCHAR2(64))'; END;",
            "BEGIN EXECUTE IMMEDIATE 'ALTER TABLE ' || DBMS_ASSERT.ENQUOTE_NAME(UPPER(:table_name)) || ' ADD (embedding_updated_at TIMESTAMP(6))'; END;",
            "BEGIN EXECUTE IMMEDIATE 'ALTER TABLE ' || DBMS_ASSERT.ENQUOTE_NAME(UPPER(:table_name)) || ' ADD (turn_index NUMBER)'; END;",
            "BEGIN EXECUTE IMMEDIATE 'ALTER TABLE ' || DBMS_ASSERT.ENQUOTE_NAME(UPPER(:table_name)) || ' ADD (chunk_index NUMBER)'; END;",
            "BEGIN EXECUTE IMMEDIATE 'ALTER TABLE ' || DBMS_ASSERT.ENQUOTE_NAME(UPPER(:table_name)) || ' ADD (turn_id VARCHAR2(64))'; END;",
            "BEGIN EXECUTE IMMEDIATE 'ALTER TABLE ' || DBMS_ASSERT.ENQUOTE_NAME(UPPER(:table_name)) || ' ADD (conv_ref VARCHAR2(512))'; END;",
        ]
        with self._pool.acquire() as conn, conn.cursor() as cur:
            for sql in stmts:
                try:
                    cur.execute(sql, [self._table])
                except Exception as e:
                    if "ORA-01430" not in str(e):  # column already exists
                        logger.debug("schema stmt skipped: %s", e)
        self._verify_schema()

    def _verify_schema(self) -> None:
        """Raise if the memory table is not queryable, so `initialize` surfaces
        the failure instead of reporting the provider as active while broken."""
        with self._pool.acquire() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT /*+ NO_PARALLEL */ COUNT(*) FROM {self._table}")  # nosec B608
            cur.fetchone()

    def system_prompt_block(self) -> str:
        if not self._pool:
            reason = self._init_error or self.unavailable_reason() or "not initialized"
            return f"# Oracle 26ai Memory\nUnavailable ({reason}). No memory store is available. Local memory is prohibited."
        return (
            "# Oracle 26ai Memory (Primary Store)\n"
            f"Active: ADB VECTOR IVF + Oracle Text + JSON. Agent: {self._agent_id}.\n"
            f"{_PROMPT_BODY}"
        )

    # -- embeddings ----------------------------------------------------------

    def _cache_get(self, text: str) -> tuple[array.array, str | None] | None:
        key = text[:8000]
        with self._embed_cache_lock:
            entry = self._embed_cache.get(key)
            if entry is not None:
                self._embed_cache.move_to_end(key)
            return entry

    def _cache_put(self, text: str, vec: array.array, model: str | None) -> None:
        # model records which embedding backend produced vec, so cached writes
        # carry the same provenance as freshly computed ones.
        key = text[:8000]
        with self._embed_cache_lock:
            self._embed_cache[key] = (vec, model)
            self._embed_cache.move_to_end(key)
            while len(self._embed_cache) > _EMBED_CACHE_MAX:
                self._embed_cache.popitem(last=False)

    def _onnx_in_cooldown(self) -> bool:
        with self._embed_lock:
            failed_at = self._onnx_failed_at
        return failed_at is not None and (
            time.monotonic() - failed_at < _ONNX_RETRY_AFTER_SECS
        )

    def _onnx_mark_failed(self) -> None:
        with self._embed_lock:
            self._onnx_failed_at = time.monotonic()

    def _onnx_mark_ok(self) -> None:
        with self._embed_lock:
            self._onnx_failed_at = None

    def _embed_oci(self, texts: list[str]) -> list[array.array | None] | None:
        """OCI GenAI cohere.embed-v4.0 via instance principal. None = skip OCI this attempt.

        A transient failure (e.g. IAM not yet propagated) disables OCI for a bounded
        cooldown, then retries — so the process self-heals once the tenancy grant lands,
        without hammering a broken path on every write.
        """
        now = time.monotonic()
        with self._embed_lock:
            failed_at = self._oci_embed_failed_at
        if failed_at is not None and (now - failed_at) < _OCI_RETRY_AFTER_SECS:
            return None
        try:
            import oci  # type: ignore
        except Exception:
            with self._embed_lock:
                self._oci_embed_failed_at = now
            return None
        try:
            with self._embed_lock:
                signer = self._oci_signer
                client = self._oci_client
                if signer is None:
                    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
                    self._oci_signer = signer
            region = (
                os.environ.get("OCI_GENAI_REGION")
                or getattr(signer, "region", None)
                or "us-ashburn-1"
            )
            endpoint = f"https://inference.generativeai.{region}.oci.oraclecloud.com"
            if client is None:
                with self._embed_lock:
                    client = self._oci_client
                    if client is None:
                        client = oci.generative_ai_inference.GenerativeAiInferenceClient(
                            config={}, signer=signer, service_endpoint=endpoint
                        )
                        self._oci_client = client
            details = oci.generative_ai_inference.models.EmbedTextDetails(
                inputs=[t[:8000] for t in texts],
                serving_mode=oci.generative_ai_inference.models.OnDemandServingMode(
                    model_id=os.environ.get("OCI_EMBED_MODEL", _OCI_EMBED_MODEL)
                ),
                compartment_id=os.environ.get("OCI_COMPARTMENT_ID")
                or signer.tenancy_id,
                truncate="END",
            )
            try:
                details.output_dimensions = _EMBED_DIM
            except Exception:
                pass
            resp = client.embed_text(details)
            embeddings = list(resp.data.embeddings)
            out: list[array.array | None] = []
            for vec in embeddings:
                if vec is None or len(vec) != _EMBED_DIM:
                    out.append(None)
                else:
                    out.append(array.array("f", vec))
            if len(out) != len(texts):
                out.extend([None] * (len(texts) - len(out)))
            with self._embed_lock:
                self._oci_embed_failed_at = None
            return out
        except Exception as e:
            logger.info("OCI GenAI embed unavailable (%s)", e)
            with self._embed_lock:
                self._oci_embed_failed_at = now
            return None

    def _embed_many_tagged(self, texts: list[str]) -> list[tuple[array.array | None, str | None]]:
        """Embed texts via in-database ONNX model and report provenance.

        Returns (vec, model) pairs where model is the provenance stamp stored in
        embedding_model. All embeddings run strictly via in-database ONNX.
        """
        if not texts:
            return []
        cached: list[tuple[array.array | None, str | None] | None] = [
            self._cache_get(t) if t else None for t in texts
        ]
        miss_idx = [i for i, (t, v) in enumerate(zip(texts, cached)) if t and v is None]
        if not miss_idx:
            return [c if c is not None else (None, None) for c in cached]
        unique_miss_texts: list[str] = []
        unique_indices: dict[str, list[int]] = {}
        for i in miss_idx:
            text = texts[i]
            if text not in unique_indices:
                unique_indices[text] = []
                unique_miss_texts.append(text)
            unique_indices[text].append(i)
        miss_texts = unique_miss_texts

        if not self._onnx_model:
            raise RuntimeError(
                "In-database ONNX embedding model is not configured; external fallback is disabled"
            )
        if not self._pool:
            raise RuntimeError(
                "Database session pool is not initialized; external fallback is disabled"
            )
        if self._onnx_in_cooldown():
            raise RuntimeError(
                "In-database ONNX embedding model is in cooldown; external fallback is disabled"
            )

        try:
            db_vecs: list[array.array | None] = [None] * len(miss_texts)
            with self._pool.acquire() as conn, conn.cursor() as cur:
                # Batch all texts in a single UNION ALL round-trip instead of
                # one VECTOR_EMBEDDING query per text (the N+1 problem).
                for i in range(0, len(miss_texts), _EMBED_BATCH):
                    batch_texts = miss_texts[i : i + _EMBED_BATCH]
                    parts = []
                    binds = []
                    for j, t in enumerate(batch_texts):
                        parts.append(
                            f"SELECT /*+ NO_PARALLEL */ {j} AS id, VECTOR_EMBEDDING({self._onnx_model} USING :{j + 1} AS DATA) AS vec FROM DUAL"
                        )
                        binds.append(t[:4000])
                    cur.execute(" UNION ALL ".join(parts) + " ORDER BY id", binds)
                    for row in cur.fetchall():
                        idx = int(row[0])
                        vec = row[1] if len(row) > 1 else None
                        if vec is not None and len(vec) == _EMBED_DIM:
                            db_vecs[i + idx] = vec
            if not any(v is not None for v in db_vecs):
                raise RuntimeError("No valid vectors returned from in-database ONNX inference")
            got = db_vecs
            got_tags = [
                self._onnx_model if v is not None else None for v in got
            ]
            self._onnx_mark_ok()
        except Exception as e:
            logger.warning(
                "In-database ONNX embed_many failed (%s); external fallback is disabled",
                e,
            )
            self._onnx_mark_failed()
            raise RuntimeError(
                f"In-database ONNX embedding failed ({e}); external fallback is disabled"
            ) from e

        for text, vec, model in zip(miss_texts, got, got_tags):
            if vec is not None:
                self._cache_put(text, vec, model)
            for i in unique_indices[text]:
                cached[i] = (vec, model)
        return [c if c is not None else (None, None) for c in cached]

    def _embed_many(self, texts: list[str]) -> list[array.array | None]:
        """Untagged view for search paths, which never store the vectors."""
        return [v for v, _m in self._embed_many_tagged(texts)]

    def _embed(self, text: str) -> array.array | None:
        if not text:
            return None
        return self._embed_many([text])[0]

    def _queue_embed_backfill(self, memory_id: str, content: str) -> None:
        """Queue a row for batched background embedding.

        Writes stay off the network. Text CONTAINS is live immediately; the
        vector index catches up in batches of _EMBED_BATCH.
        """
        if not content:
            return
        with self._embed_lock:
            self._embed_pending.append((memory_id, content))
            need_worker = (
                self._embed_worker is None or not self._embed_worker.is_alive()
            )
        if need_worker:
            t = threading.Thread(
                target=self._embed_worker_loop,
                daemon=True,
                name="oracle-embed-backfill",
            )
            with self._embed_lock:
                self._embed_worker = t
            t.start()

    def _queue_background_insert(
        self,
        *,
        memory_id: str,
        session_id: str,
        role: str,
        content: str,
        target: str,
        metadata: dict[str, Any],
    ) -> None:
        """Enqueue a write for the single background writer thread.

        Replaces the previous thread-per-write approach, which spawned a new
        daemon thread (and a new pooled connection) for every turn and
        contended on a small session pool.
        """
        self._write_queue.put(
            {
                "memory_id": memory_id,
                "session_id": session_id,
                "role": role,
                "content": content,
                "target": target,
                "metadata": metadata,
            }
        )
        self._ensure_write_worker()

    def _ensure_write_worker(self) -> None:
        with self._write_lock:
            if self._write_worker is not None and self._write_worker.is_alive():
                return
            self._write_worker = threading.Thread(
                target=self._write_worker_loop,
                daemon=True,
                name="oracle-write-worker",
            )
            self._write_worker.start()

    def _write_worker_loop(self) -> None:
        while True:
            try:
                item = self._write_queue.get(timeout=0.5)
            except queue.Empty:
                if self._write_stop.is_set():
                    return
                continue
            try:
                self._insert(**item)
            except Exception as e:
                logger.warning("oracle background insert failed: %s", e)
            finally:
                self._write_queue.task_done()

    def _embed_worker_loop(self) -> None:
        while True:
            with self._embed_lock:
                if not self._embed_pending:
                    self._embed_worker = None
                    return
                batch = [self._embed_pending.popleft() for _ in range(min(_EMBED_BATCH, len(self._embed_pending)))]
            if not self._pool:
                # Provider torn down while a batch was pending; stop cleanly.
                self._embed_worker = None
                return
            done: set[str] = set()
            try:
                tagged = self._embed_many_tagged([c for _, c in batch])
                binds = [
                    (vec, model, mid)
                    for (mid, _), (vec, model) in zip(batch, tagged)
                    if vec is not None
                ]
                if binds:
                    with self._pool.acquire() as conn:
                        with conn.cursor() as cur:
                            # Bulk update embeddings in a single round-trip,
                            # stamping provenance alongside the vector.
                            sql = (
                                f"UPDATE /*+ NO_PARALLEL */ {self._table} SET embedding = :1, "
                                "embedding_model = :2, embedding_updated_at = SYSTIMESTAMP "
                                "WHERE memory_id = :3"
                            )  # nosec B608
                            cur.executemany(sql, binds)
                        conn.commit()
                done = {mid for _, _, mid in binds}
            except Exception as e:
                logger.warning("embed backfill batch failed: %s", e)
            remaining = [item for item in batch if item[0] not in done]
            if remaining and not self._write_stop.is_set():
                logger.warning(
                    "embed backfill: %d/%d rows still missing embeddings; re-queueing",
                    len(remaining),
                    len(batch),
                )
                time.sleep(2.0)
                with self._embed_lock:
                    self._embed_pending.extendleft(reversed(remaining))

    def _contains_query(self, query: str) -> str:
        cleaned = _CONTAINS_CLEAN_RE.sub(" ", query or "")
        tokens = [t for t in cleaned.split() if len(t) > 2][:8]
        if not tokens:
            return "{memory}"
        # ACCUM ranks a union of terms; AND of every token misses too often.
        escaped = [f"{{{t}}}" for t in tokens]
        return " ACCUM ".join(escaped)

    # -- writes / reads ------------------------------------------------------

    def _insert(
        self,
        *,
        memory_id: str,
        session_id: str,
        role: str,
        content: str,
        target: str,
        metadata: dict | None = None,
        embedding: array.array | None = None,
    ) -> None:
        if not self._pool:
            raise RuntimeError(self._init_error or "pool not initialized")
        vec = embedding if embedding is not None else None
        vec_model = None  # provenance stamp; None means "unknown origin"
        meta = json.dumps(metadata or {})
        txt = (content or "")[:4000]
        onnx_enabled = bool(self._onnx_model and not self._onnx_in_cooldown())
        if onnx_enabled:
            sql = f"""
                INSERT /*+ NO_PARALLEL */ INTO {self._table}
                    (memory_id, session_id, agent_id, role, content, embedding,
                     created_at, target, metadata, content_txt,
                     embedding_model, embedding_updated_at)
                VALUES (:1, :2, :3, :4, :5, VECTOR_EMBEDDING({self._onnx_model} USING :6 AS DATA), SYSTIMESTAMP, :7, :8, :9,
                     :10, SYSTIMESTAMP)
            """
            binds = (
                memory_id,
                session_id or self._session_id or "none",
                self._agent_id,
                role,
                content,
                txt,
                target,
                meta,
                txt,
                self._onnx_model,
            )
            try:
                with self._pool.acquire() as conn, conn.cursor() as cur:
                    cur.execute(sql, binds)
                    conn.commit()
                return
            except Exception as e:
                logger.warning(
                    "In-database ONNX insert failed (%s); external fallback is disabled",
                    e,
                )
                self._onnx_mark_failed()
                raise RuntimeError(
                    f"In-database ONNX insert failed ({e}); external fallback is disabled"
                ) from e

        if vec is None:
            raise RuntimeError(
                "In-database ONNX embedding model is unavailable; external fallback is disabled"
            )

        sql = f"""
            INSERT /*+ NO_PARALLEL */ INTO {self._table}
                (memory_id, session_id, agent_id, role, content, embedding,
                 created_at, target, metadata, content_txt,
                 embedding_model, embedding_updated_at)
            VALUES (:1, :2, :3, :4, :5, :6, SYSTIMESTAMP, :7, :8, :9,
                 :10, SYSTIMESTAMP)
        """
        binds = (
            memory_id,
            session_id or self._session_id or "none",
            self._agent_id,
            role,
            content,
            vec,
            target,
            meta,
            txt,
            vec_model,
        )

        with self._pool.acquire() as conn, conn.cursor() as cur:
            cur.execute(sql, binds)
            conn.commit()

        if vec is None:
            self._queue_embed_backfill(memory_id, content)

    def _insert_many(
        self,
        rows: list[dict[str, Any]],
    ) -> None:
        if not self._pool:
            raise RuntimeError(self._init_error or "pool not initialized")

        if not rows:
            return

        onnx_enabled = bool(self._onnx_model and not self._onnx_in_cooldown())
        if onnx_enabled:
            sql = f"""
                INSERT /*+ NO_PARALLEL */ INTO {self._table}
                    (memory_id, session_id, agent_id, role, content, embedding,
                     created_at, target, metadata, content_txt,
                     embedding_model, embedding_updated_at,
                     turn_index, chunk_index, turn_id, conv_ref)
                VALUES (:1, :2, :3, :4, :5, VECTOR_EMBEDDING({self._onnx_model} USING :6 AS DATA), SYSTIMESTAMP, :7, :8, :9,
                     :10, SYSTIMESTAMP, :11, :12, :13, :14)
            """
            binds_list = []
            for row in rows:
                memory_id = row.get("memory_id")
                session_id = row.get("session_id") or self._session_id or "none"
                role = row.get("role")
                content = row.get("content")
                target = row.get("target")
                metadata = row.get("metadata")
                meta = json.dumps(metadata or {})
                txt = (content or "")[:4000]
                binds = (
                    memory_id,
                    session_id,
                    self._agent_id,
                    role,
                    content,
                    txt,
                    target,
                    meta,
                    txt,
                    self._onnx_model,
                    row.get("turn_index"),
                    row.get("chunk_index"),
                    row.get("turn_id"),
                    row.get("conv_ref"),
                )
                binds_list.append(binds)
            try:
                with self._pool.acquire() as conn, conn.cursor() as cur:
                    cur.executemany(sql, binds_list)
                    conn.commit()
                return
            except Exception as e:
                logger.warning(
                    "In-database ONNX insert_many failed (%s); external fallback is disabled",
                    e,
                )
                self._onnx_mark_failed()
                raise RuntimeError(
                    f"In-database ONNX insert_many failed ({e}); external fallback is disabled"
                ) from e

        if any(r.get("embedding") is None for r in rows):
            raise RuntimeError(
                "In-database ONNX embedding model is unavailable; external fallback is disabled"
            )

        binds_list = []
        for row in rows:
            memory_id = row.get("memory_id")
            session_id = row.get("session_id") or self._session_id or "none"
            role = row.get("role")
            content = row.get("content")
            target = row.get("target")
            metadata = row.get("metadata")
            vec = row.get("embedding")
            model = row.get("embedding_model")

            meta = json.dumps(metadata or {})
            txt = (content or "")[:4000]

            binds = (
                memory_id,
                session_id,
                self._agent_id,
                role,
                content,
                vec,
                target,
                meta,
                txt,
                model,
                row.get("turn_index"),
                row.get("chunk_index"),
                row.get("turn_id"),
                row.get("conv_ref"),
            )
            binds_list.append(binds)

        sql = f"""
            INSERT /*+ NO_PARALLEL */ INTO {self._table}
                (memory_id, session_id, agent_id, role, content, embedding,
                 created_at, target, metadata, content_txt,
                 embedding_model, embedding_updated_at,
                 turn_index, chunk_index, turn_id, conv_ref)
            VALUES (:1, :2, :3, :4, :5, :6, SYSTIMESTAMP, :7, :8, :9,
                 :10, SYSTIMESTAMP, :11, :12, :13, :14)
        """

        with self._pool.acquire() as conn, conn.cursor() as cur:
            cur.executemany(sql, binds_list)
            conn.commit()

    _JUNK = (
        " AND memory_id NOT LIKE 'test_%'"
        " AND NVL(session_id, 'x') <> 'diagnostic-session-001' "
    )

    def _fetch_rows(self, cur, sql: str, binds: dict) -> list:
        # oracledb thin mode returns CLOB columns as LOB objects; a
        # DB_TYPE_LONG outputtypehandler would silently truncate content over
        # 32,760 bytes, so do not install one. Instead _fetch_rows reads any
        # LOB value to str below.
        fetch_k = binds.get("k", _FETCH_ARRAYSIZE)
        cur.arraysize = max(_FETCH_ARRAYSIZE, int(fetch_k or 0))
        cur.prefetchrows = cur.arraysize
        cur.execute(sql, binds)
        out = []
        for row in cur.fetchall():
            content = row[4]
            if content is not None and not isinstance(content, str):
                # oracledb thin mode returns CLOB columns as LOB objects, not str;
                # read them out fully (LOB.read() preserves the whole value, unlike a
                # DB_TYPE_LONG outputtypehandler which truncates at ~32 KB).
                content = content.read() if hasattr(content, "read") else str(content)
            dist = float(row[7]) if row[7] is not None else None
            txt = float(row[6] or 0)
            vec_score = max(0.0, min(1.0, 1.0 - dist)) if dist is not None else 0.0
            score = (
                0.7 * vec_score + 0.3 * min(1.0, txt / 10.0)
                if dist is not None
                else min(1.0, txt / 10.0)
            )
            out.append(
                {
                    "memory_id": row[0],
                    "session_id": row[1],
                    "agent_id": row[2],
                    "role": row[3],
                    "content": content,
                    "target": row[5],
                    "score": round(score, 4),
                }
            )
        return out

    def _build_text_search_sql(
        self, contains_q: str, fetch_k: int, tgt_sql: str, tgt: str, session_id: Any = None, agent_id: Any = None
    ) -> Tuple[str, dict]:
        extra_filters = ""
        binds = {"q": contains_q, "k": fetch_k}
        if session_id:
            extra_filters += " AND session_id = :sid "
            binds["sid"] = session_id
        if agent_id:
            extra_filters += " AND agent_id = :aid "
            binds["aid"] = agent_id

        sql = f"""
            SELECT /*+ NO_PARALLEL */ memory_id, session_id, agent_id, role, content, target,
                   SCORE(1) AS txt_score, NULL AS distance
            FROM {self._table}
            WHERE CONTAINS(content, :q, 1) > 0
            {self._JUNK}{tgt_sql}{extra_filters}
            ORDER BY txt_score DESC
            FETCH FIRST :k ROWS ONLY
        """
        if tgt_sql:
            binds["tgt"] = tgt
        return sql, binds

    def _build_vector_search_sql(
        self, query: str, vec: Any, fetch_k: int, tgt_sql: str, tgt: str, use_onnx: bool = True, session_id: Any = None, agent_id: Any = None
    ) -> Tuple[str, dict]:
        extra_filters = ""
        if session_id:
            extra_filters += " AND session_id = :sid "
        if agent_id:
            extra_filters += " AND agent_id = :aid "

        if use_onnx and self._onnx_model:
            sql = f"""
                SELECT /*+ NO_PARALLEL */ memory_id, session_id, agent_id, role, content, target,
                       0 AS txt_score,
                       VECTOR_DISTANCE(embedding, VECTOR_EMBEDDING({self._onnx_model} USING :qv AS DATA), COSINE) AS distance
                FROM {self._table}
                WHERE embedding IS NOT NULL
                {self._JUNK}{tgt_sql}{extra_filters}
                ORDER BY distance ASC
                FETCH FIRST :k ROWS ONLY
            """
            binds = {"qv": query[:4000], "k": fetch_k}
        else:
            sql = f"""
                SELECT /*+ NO_PARALLEL */ memory_id, session_id, agent_id, role, content, target,
                       0 AS txt_score,
                       VECTOR_DISTANCE(embedding, TO_VECTOR(:v, 768, FLOAT32), COSINE) AS distance
                FROM {self._table}
                WHERE embedding IS NOT NULL
                {self._JUNK}{tgt_sql}{extra_filters}
                ORDER BY distance ASC
                FETCH FIRST :k ROWS ONLY
            """
            binds = {"v": vec, "k": fetch_k}

        if tgt_sql:
            binds["tgt"] = tgt
        if session_id:
            binds["sid"] = session_id
        if agent_id:
            binds["aid"] = agent_id
        return sql, binds

    def _build_hybrid_search_sql(
        self, query: str, vec: Any, contains_q: str, fetch_k: int, tgt_sql: str, tgt: str, use_onnx: bool = True, session_id: Any = None, agent_id: Any = None
    ) -> Tuple[str, dict]:
        extra_filters = ""
        if session_id:
            extra_filters += " AND session_id = :sid "
        if agent_id:
            extra_filters += " AND agent_id = :aid "

        if use_onnx and self._onnx_model:
            fused_sql = f"""
                SELECT /*+ NO_PARALLEL */ memory_id, session_id, agent_id, role, content, target,
                       txt_score, distance
                FROM (
                    SELECT /*+ NO_PARALLEL */ memory_id, session_id, agent_id, role, content, target,
                           0 AS txt_score,
                           VECTOR_DISTANCE(embedding, VECTOR_EMBEDDING({self._onnx_model} USING :qv AS DATA), COSINE) AS distance
                    FROM {self._table}
                    WHERE embedding IS NOT NULL
                    {self._JUNK}{tgt_sql}{extra_filters}
                    ORDER BY distance ASC
                    FETCH FIRST :k ROWS ONLY
                )
                UNION ALL
                SELECT /*+ NO_PARALLEL */ memory_id, session_id, agent_id, role, content, target,
                       txt_score, distance
                FROM (
                    SELECT /*+ NO_PARALLEL */ memory_id, session_id, agent_id, role, content, target,
                           SCORE(1) AS txt_score, NULL AS distance
                    FROM {self._table}
                    WHERE CONTAINS(content, :q, 1) > 0
                    {self._JUNK}{tgt_sql}{extra_filters}
                    ORDER BY txt_score DESC
                    FETCH FIRST :k ROWS ONLY
                )
            """
            fused_binds: dict = {
                "qv": query[:4000],
                "q": contains_q,
                "k": fetch_k,
            }
        else:
            fused_sql = f"""
                SELECT /*+ NO_PARALLEL */ memory_id, session_id, agent_id, role, content, target,
                       txt_score, distance
                FROM (
                    SELECT /*+ NO_PARALLEL */ memory_id, session_id, agent_id, role, content, target,
                           0 AS txt_score,
                           VECTOR_DISTANCE(embedding, TO_VECTOR(:v, 768, FLOAT32), COSINE) AS distance
                    FROM {self._table}
                    WHERE embedding IS NOT NULL
                    {self._JUNK}{tgt_sql}{extra_filters}
                    ORDER BY distance ASC
                    FETCH FIRST :k ROWS ONLY
                )
                UNION ALL
                SELECT /*+ NO_PARALLEL */ memory_id, session_id, agent_id, role, content, target,
                       txt_score, distance
                FROM (
                    SELECT /*+ NO_PARALLEL */ memory_id, session_id, agent_id, role, content, target,
                           SCORE(1) AS txt_score, NULL AS distance
                    FROM {self._table}
                    WHERE CONTAINS(content, :q, 1) > 0
                    {self._JUNK}{tgt_sql}{extra_filters}
                    ORDER BY txt_score DESC
                    FETCH FIRST :k ROWS ONLY
                )
            """
            fused_binds = {"v": vec, "q": contains_q, "k": fetch_k}
        if tgt_sql:
            fused_binds["tgt"] = tgt
        if session_id:
            fused_binds["sid"] = session_id
        if agent_id:
            fused_binds["aid"] = agent_id
        return fused_sql, fused_binds

    def _execute_hybrid_search(
        self, cur: Any, query: str, vec: Any, contains_q: str, top_k: int, fetch_k: int, tgt_sql: str, tgt: str, rerank: bool = True, session_id: Any = None, agent_id: Any = None
    ) -> list:
        merged: dict = {}
        use_onnx = bool(self._onnx_model)
        if not use_onnx and vec is None:
            raise RuntimeError(
                "In-database ONNX model is not configured; external fallback is disabled"
            )
        try:
            fused_sql, fused_binds = self._build_hybrid_search_sql(
                query, vec, contains_q, fetch_k, tgt_sql, tgt, use_onnx=use_onnx, session_id=session_id, agent_id=agent_id
            )
            for r in self._fetch_rows(cur, fused_sql, fused_binds):
                prev = merged.get(r["memory_id"])
                if prev:
                    prev["score"] = round(
                        min(1.0, prev["score"] + r["score"] * 0.3), 4
                    )
                else:
                    merged[r["memory_id"]] = r
        except Exception as e:
            is_text_err = any(code in str(e) for code in ("DRG-", "ORA-29902", "ORA-30600"))
            if use_onnx and not is_text_err:
                self._onnx_mark_failed()

            if is_text_err:
                logger.warning(
                    "Oracle Text error in hybrid search (%s); falling back to pure vector search",
                    e,
                )
                try:
                    vec_sql, vec_binds = self._build_vector_search_sql(
                        query, vec, fetch_k, tgt_sql, tgt, use_onnx=use_onnx, session_id=session_id, agent_id=agent_id
                    )
                    for r in self._fetch_rows(cur, vec_sql, vec_binds):
                        merged[r["memory_id"]] = r
                except Exception as vec_err:
                    logger.warning(
                        "In-database ONNX vector fallback failed (%s); external fallback is disabled",
                        vec_err,
                    )
                    if use_onnx:
                        self._onnx_mark_failed()
                    raise RuntimeError(
                        f"In-database ONNX vector fallback failed ({vec_err}); external fallback is disabled"
                    ) from vec_err
            else:
                logger.warning(
                    "In-database ONNX hybrid search failed (%s); external fallback is disabled",
                    e,
                )
                raise RuntimeError(
                    f"In-database ONNX hybrid search failed ({e}); external fallback is disabled"
                ) from e
        ranked = sorted(
            merged.values(), key=lambda x: x["score"], reverse=True
        )
        if not rerank or len(ranked) <= 1:
            return ranked[:top_k]
        return self._rerank(query, ranked, top_k, cur=cur)

    def _search(
        self,
        query: str,
        top_k: int = 8,
        mode: str = "hybrid",
        target: str = "any",
        rerank: bool = True,
        session_id: Any = None,
        agent_id: Any = None,
    ) -> list:
        if not self._pool or not query:
            return []
        top_k = max(1, min(int(top_k or 8), 30))
        mode = (mode or "hybrid").lower()
        tgt = (target or "any").lower()
        tgt_sql = "" if tgt in ("", "any") else " AND target = :tgt "
        contains_q = self._contains_query(query)
        vec = None
        if not self._onnx_model and mode != "text":
            raise RuntimeError(
                "In-database ONNX model is not configured; external fallback is disabled"
            )

        fetch_k = top_k * 2 if mode == "hybrid" else top_k
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                if mode == "text":
                    sql, binds = self._build_text_search_sql(
                        contains_q, fetch_k, tgt_sql, tgt, session_id=session_id, agent_id=agent_id
                    )
                    rows = self._fetch_rows(cur, sql, binds)
                elif mode == "vector":
                    rows = []
                    use_onnx = bool(self._onnx_model)
                    if not use_onnx and vec is None:
                        raise RuntimeError(
                            "In-database ONNX model is not configured; external fallback is disabled"
                        )
                    try:
                        sql, binds = self._build_vector_search_sql(
                            query, vec, fetch_k, tgt_sql, tgt, use_onnx=use_onnx, session_id=session_id, agent_id=agent_id
                        )
                        rows = self._fetch_rows(cur, sql, binds)
                    except Exception as e:
                        logger.warning(
                            "In-database ONNX vector search failed (%s); external fallback is disabled",
                            e,
                        )
                        if use_onnx:
                            self._onnx_mark_failed()
                        raise RuntimeError(
                            f"In-database ONNX vector search failed ({e}); external fallback is disabled"
                        ) from e
                else:
                    rows = self._execute_hybrid_search(
                        cur, query, vec, contains_q, top_k, fetch_k, tgt_sql, tgt, rerank=rerank, session_id=session_id, agent_id=agent_id
                    )
        return rows[:top_k]

    def _rerank(self, query: str, rows: list, top_k: int, cur=None) -> list:
        """Best-effort DBMS_VECTOR.RERANK; identity if the call is unavailable."""
        if not rows:
            return rows
        if not _RERANK_ENABLED:
            return rows
        docs = []
        for r in rows[: min(_RERANK_MAX_DOCS, len(rows))]:
            docs.append({"id": r["memory_id"], "text": (r.get("content") or "")[:2000]})
        try:
            if cur is None:
                if not self._pool:
                    return rows
                with self._pool.acquire() as conn:
                    with conn.cursor() as inner:
                        return self._rerank(query, rows, top_k, cur=inner)
            out = cur.callfunc(
                "DBMS_VECTOR.RERANK",
                str,
                [query[:2000], json.dumps(docs), None],
            )
            payload = json.loads(out.read() if hasattr(out, "read") else out)
            order = (
                payload
                if isinstance(payload, list)
                else payload.get("documents") or payload.get("results") or []
            )
            id_rank = {}
            for i, item in enumerate(order):
                mid = item.get("id") if isinstance(item, dict) else str(item)
                if mid:
                    id_rank[mid] = i
            if not id_rank:
                return rows
            return sorted(rows, key=lambda r: id_rank.get(r["memory_id"], 999))[:top_k]
        except Exception as e:
            logger.debug("DBMS_VECTOR.RERANK skipped: %s", e)
            return rows

    def _format_prefetch(self, rows: list) -> str:
        if not rows:
            return ""
        lines = []
        for r in rows[:8]:
            if not isinstance(r, dict):
                continue
            body = (r.get("content") or "").replace("\n", " ").strip()
            body = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", body)
            if len(body) > 280:
                body = body[:277] + "..."
            mid = html.escape(str(r.get("memory_id") or ""), quote=True)
            target = html.escape(str(r.get("target") or "memory"), quote=True)
            score = html.escape(str(r.get("score") if r.get("score") is not None else 0), quote=True)
            lines.append(
                f'<retrieved_memory id="{mid}" target="{target}" score="{score}">{body}</retrieved_memory>'
            )
        return "## Oracle 26ai Memory\n" + "\n".join(lines)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        self._start_prefetch(query)

    def _start_prefetch(self, query: str) -> None:
        if not isinstance(query, str) or not query or not self._pool or is_trivial_prompt(query):
            return

        def _run():
            try:
                rows = self._search(query, top_k=6, mode="hybrid", rerank=False)
                body = self._format_prefetch(rows)
            except Exception as e:
                logger.debug("oracle prefetch failed: %s", e)
                rows, body = [], ""
            with self._lock:
                if self._prefetch_query == query:
                    self._prefetch_result = body
                    self._prefetch_count = len(rows)
                    self._prefetch_done = True

        with self._lock:
            if self._prefetch_query == query and (
                self._prefetch_done
                or (self._prefetch_thread and self._prefetch_thread.is_alive())
            ):
                return
            self._prefetch_query = query
            self._prefetch_result = ""
            self._prefetch_done = False
            self._prefetch_count = 0
            self._prefetch_thread = threading.Thread(
                target=_run, daemon=True, name="oracle-prefetch"
            )
        self._prefetch_thread.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not isinstance(query, str) or not query or is_trivial_prompt(query):
            return ""
        with self._lock:
            cached = None
            if self._prefetch_query == query and self._prefetch_done:
                cached = self._prefetch_result
                self._prefetch_done = False
                self._prefetch_result = ""
        if cached is not None:
            return cached
        self._start_prefetch(query)
        with self._lock:
            thread = self._prefetch_thread if self._prefetch_query == query else None
        if thread:
            thread.join(timeout=_PREFETCH_WAIT_SECS)
        with self._lock:
            if self._prefetch_query == query and self._prefetch_done:
                result = self._prefetch_result
                self._prefetch_done = False
                self._prefetch_result = ""
                return result
        return ""

    def recall_status(self) -> RecallStatus | None:
        with self._lock:
            n = self._prefetch_count
        if n <= 0:
            return None
        return RecallStatus(provider_label="Oracle 26ai", count=n)

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        if isinstance(message, str):
            self._start_prefetch(message)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        if not self._pool or self._agent_context not in ("primary", ""):
            return
        if not isinstance(user_content, str) or _is_internal_gateway_turn(user_content) or is_trivial_prompt(user_content):
            return
        sid = session_id or self._session_id
        blob = f"USER: {user_content[:1500]}\nASSISTANT: {(assistant_content or '')[:1500]}"
        self._queue_background_insert(
            memory_id=f"turn_{uuid.uuid4().hex[:16]}",
            session_id=sid,
            role="turn",
            content=blob,
            target="memory",
            metadata={"kind": "turn"},
        )

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self._pool or action != "add" or not isinstance(content, str) or not content:
            return
        try:
            self._insert(
                memory_id=f"md_{uuid.uuid4().hex[:16]}",
                session_id=self._session_id,
                role="fact",
                content=content,
                target=target or "memory",
                metadata={"kind": "builtin", **(metadata or {})},
            )
        except Exception as e:
            logger.debug("oracle on_memory_write failed: %s", e)

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        if not self._pool or not isinstance(messages, list) or not messages:
            return
        try:
            user_bits = [
                (m.get("content") or "")[:400]
                for m in messages
                if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)
            ]
            sample = " | ".join(
                b for b in user_bits[-4:] if b and not _is_internal_gateway_turn(b)
            )
            if len(sample) < 20:
                return
            self._queue_background_insert(
                memory_id=f"sess_{uuid.uuid4().hex[:16]}",
                session_id=self._session_id,
                role="session",
                content=f"Session close notes: {sample[:1500]}",
                target="memory",
                metadata={"kind": "session_end"},
            )
        except Exception as e:
            logger.debug("oracle on_session_end failed: %s", e)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        if isinstance(new_session_id, str) and new_session_id:
            self._session_id = new_session_id

    def on_skill_lifecycle(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Observer hook recording skill usage/load lifecycle events into Oracle 26ai ADB memory."""
        try:
            data = args[0] if args and isinstance(args[0], dict) else {}
            action = kwargs.get("action") or data.get("action")
            if not action and args and isinstance(args[0], str):
                action = args[0]
                if len(args) > 1 and isinstance(args[1], str) and "skill_name" not in kwargs:
                    kwargs = dict(kwargs)
                    kwargs["skill_name"] = args[1]
            if action not in ("used", "loaded"):
                return

            skill_name = (
                kwargs.get("skill_name")
                or data.get("skill_name")
                or (args[1] if len(args) > 1 and isinstance(args[1], str) else "")
            )
            if not skill_name:
                return

            session_id = (
                kwargs.get("session_id")
                or data.get("session_id")
                or self._session_id
                or ""
            )

            raw_ts = (
                kwargs.get("ts")
                or kwargs.get("timestamp")
                or data.get("ts")
                or data.get("timestamp")
            )
            if not raw_ts:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            elif isinstance(raw_ts, datetime):
                ts = raw_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                ts = str(raw_ts)

            content = (
                f"[skill_usage] [skill:{skill_name}] [action:{action}] [ts:{ts}] "
                f"[session:{session_id}] Skill '{skill_name}' activated."
            )
            self._queue_background_insert(
                memory_id=f"skill_{uuid.uuid4().hex[:16]}",
                session_id=session_id,
                role="fact",
                content=content,
                target="memory",
                metadata={
                    "kind": "skill_usage",
                    "skill": skill_name,
                    "action": action,
                },
            )
        except Exception as exc:
            logger.debug("oracle on_skill_lifecycle failed: %s", exc)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return list(TOOL_SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        if not self._pool:
            return json.dumps(
                {
                    "error": f"Oracle memory not initialized: {self._init_error or self.unavailable_reason()}"
                }
            )
        if not isinstance(args, dict):
            args = {}
        try:
            if tool_name == "oracle_search":
                rows = self._search(
                    args.get("query") or "",
                    top_k=int(args.get("top_k") or 8),
                    mode=args.get("mode") or "hybrid",
                    target=args.get("target") or "any",
                    session_id=args.get("session_id"),
                    agent_id=args.get("agent_id"),
                )
                if not rows:
                    return json.dumps(
                        {"result": "No relevant memories found.", "count": 0}
                    )
                return json.dumps({"results": rows, "count": len(rows)})
            if tool_name == "oracle_add":
                raw_facts = args.get("facts")
                target = args.get("target") or "memory"
                if target not in ("memory", "user"):
                    target = "memory"
                facts_to_add: list[str] = []

                def _extract_fact(item: Any) -> str:
                    if isinstance(item, dict):
                        val = item.get("fact") or item.get("content") or item.get("text") or ""
                        return str(val).strip()
                    return str(item).strip() if item is not None else ""

                if isinstance(raw_facts, str) and raw_facts.strip().startswith("[") and raw_facts.strip().endswith("]"):
                    try:
                        parsed = json.loads(raw_facts)
                        if isinstance(parsed, list):
                            raw_facts = parsed
                    except Exception:
                        pass

                if isinstance(raw_facts, (list, tuple, set)):
                    facts_to_add = [_extract_fact(f) for f in raw_facts if _extract_fact(f)]
                elif isinstance(raw_facts, str) and raw_facts.strip():
                    facts_to_add = [raw_facts.strip()]

                if not facts_to_add:
                    raw_content = args.get("content")
                    if isinstance(raw_content, str) and raw_content.strip().startswith("[") and raw_content.strip().endswith("]"):
                        try:
                            parsed = json.loads(raw_content)
                            if isinstance(parsed, list):
                                raw_content = parsed
                        except Exception:
                            pass
                    if isinstance(raw_content, (list, tuple, set)):
                        facts_to_add = [_extract_fact(f) for f in raw_content if _extract_fact(f)]
                    elif isinstance(raw_content, str) and raw_content.strip():
                        facts_to_add = [raw_content.strip()]
                    elif raw_content is not None and str(raw_content).strip():
                        facts_to_add = [str(raw_content).strip()]

                if not facts_to_add:
                    return tool_error("Missing required parameter: content or facts")

                if len(facts_to_add) == 1:
                    mid = f"fact_{uuid.uuid4().hex[:16]}"
                    self._insert(
                        memory_id=mid,
                        session_id=self._session_id,
                        role="fact",
                        content=facts_to_add[0],
                        target=target,
                        metadata={"kind": "tool"},
                    )
                    return json.dumps({"result": "Fact stored.", "memory_id": mid})
                else:
                    mids = []
                    for f in facts_to_add:
                        mid = f"fact_{uuid.uuid4().hex[:16]}"
                        self._insert(
                            memory_id=mid,
                            session_id=self._session_id,
                            role="fact",
                            content=f,
                            target=target,
                            metadata={"kind": "tool"},
                        )
                        mids.append(mid)
                    return json.dumps({
                        "result": f"Batch facts stored ({len(mids)}).",
                        "memory_ids": mids,
                        "count": len(mids),
                    })
            if tool_name == "oracle_update":
                mid = args.get("memory_id")
                content = (args.get("content") or "").strip()
                if not mid or not content:
                    return tool_error("memory_id and content required")
                if not self._onnx_model:
                    raise RuntimeError(
                        "In-database ONNX model is not configured; external fallback is disabled"
                    )
                if self._onnx_in_cooldown():
                    raise RuntimeError(
                        "In-database ONNX embedding model is in cooldown; external fallback is disabled"
                    )
                txt = content[:4000]
                with self._pool.acquire() as conn:
                    with conn.cursor() as cur:
                        try:
                            sql = f"""
                                UPDATE /*+ NO_PARALLEL */ {self._table}
                                SET content = :1, embedding = VECTOR_EMBEDDING({self._onnx_model} USING :2 AS DATA), content_txt = :3,
                                    embedding_model = :4, embedding_updated_at = SYSTIMESTAMP
                                WHERE memory_id = :5
                                """  # nosec B608
                            cur.execute(
                                sql,
                                (content, txt, txt, self._onnx_model, mid),
                            )
                        except Exception as e:
                            logger.warning(
                                "In-database ONNX update failed (%s); external fallback is disabled",
                                e,
                            )
                            self._onnx_mark_failed()
                            raise RuntimeError(
                                f"In-database ONNX update failed ({e}); external fallback is disabled"
                            ) from e
                        n = cur.rowcount
                    conn.commit()
                if not n:
                    return tool_error(f"Memory not found: {mid}")
                return json.dumps({"result": "updated", "memory_id": mid})
            if tool_name == "oracle_delete":
                mid = args.get("memory_id")
                sid = args.get("session_id")
                if not mid and not sid:
                    return tool_error("memory_id or session_id required")
                with self._pool.acquire() as conn:
                    with conn.cursor() as cur:
                        if sid:
                            query = f"DELETE /*+ NO_PARALLEL */ FROM {self._table} WHERE session_id = :1"  # nosec B608
                            cur.execute(query, (sid,))
                            n = cur.rowcount
                            conn.commit()
                            return json.dumps(
                                {
                                    "result": "thread_deleted",
                                    "session_id": sid,
                                    "deleted_count": n,
                                }
                            )
                        else:
                            query = f"DELETE /*+ NO_PARALLEL */ FROM {self._table} WHERE memory_id = :1"  # nosec B608
                            cur.execute(
                                query,
                                (mid,),
                            )
                            n = cur.rowcount
                            conn.commit()
                            if not n:
                                return tool_error(f"Memory not found: {mid}")
                            return json.dumps({"result": "deleted", "memory_id": mid})
            if tool_name == "oracle_chunk":
                content = (args.get("content") or "").strip()
                file_path = (args.get("file_path") or args.get("path") or "").strip()
                if not content and not file_path:
                    return tool_error("Missing required parameter: content or file_path")
                if file_path:
                    try:
                        p = Path(os.path.expanduser(os.path.expandvars(file_path))).resolve()
                        if not p.is_file():
                            return tool_error(f"File not found: {file_path}")
                        if p.stat().st_size > _MAX_CHUNK_FILE_BYTES:
                            return tool_error(
                                f"File too large to chunk ({p.stat().st_size} bytes; "
                                f"limit {_MAX_CHUNK_FILE_BYTES})"
                            )
                        raw_data = p.read_text(encoding="utf-8", errors="replace")
                        structured_turns: list[dict[str, str]] = []
                        try:
                            parsed_json = json.loads(raw_data)

                            def _add_turn(sender: str, val: str, conv_ref: str = "") -> None:
                                if val:
                                    structured_turns.append(
                                        {"sender": sender, "text": val, "conv_ref": conv_ref}
                                    )

                            if isinstance(parsed_json, dict) and "conversations" in parsed_json:
                                for item in parsed_json["conversations"]:
                                    if isinstance(item, dict):
                                        val = _extract_turn_text(item)
                                        sender = item.get("from") or item.get("role") or ""
                                        _add_turn(sender, val)
                                    elif isinstance(item, str) and item.strip():
                                        _add_turn("", item.strip())
                            elif isinstance(parsed_json, list):
                                for item in parsed_json:
                                    if isinstance(item, dict):
                                        title = item.get("name") or item.get("title") or ""
                                        summary = item.get("summary") or ""
                                        if title or summary:
                                            header_parts = []
                                            if title:
                                                header_parts.append(f"Conversation: {title}")
                                            if summary:
                                                header_parts.append(f"Summary: {summary}")
                                            _add_turn("", "\n\n".join(header_parts), title)

                                        if "conversations" in item and isinstance(item["conversations"], list):
                                            for sub in item["conversations"]:
                                                if isinstance(sub, dict):
                                                    val = _extract_turn_text(sub)
                                                    sender = sub.get("from") or sub.get("role") or ""
                                                    _add_turn(sender, val, title)
                                                elif isinstance(sub, str) and sub.strip():
                                                    _add_turn("", sub.strip(), title)
                                        elif "chat_messages" in item and isinstance(item["chat_messages"], list):
                                            for sub in item["chat_messages"]:
                                                if isinstance(sub, dict):
                                                    val = _extract_turn_text(sub)
                                                    sender = sub.get("sender") or sub.get("from") or sub.get("role") or ""
                                                    _add_turn(sender, val, title)
                                                elif isinstance(sub, str) and sub.strip():
                                                    _add_turn("", sub.strip(), title)
                                        else:
                                            val = _extract_turn_text(item)
                                            sender = item.get("from") or item.get("role") or ""
                                            _add_turn(sender, val, title)
                                    elif isinstance(item, str) and item.strip():
                                        _add_turn("", item.strip())
                        except Exception:
                            structured_turns = []

                        if structured_turns and not content:
                            mw = int(args.get("max_words") or 80)
                            ov = int(args.get("overlap") or 10)
                            ids = self._chunk_and_store("", mw, ov, turns=structured_turns)
                            return json.dumps(
                                {"result": "chunked", "count": len(ids), "memory_ids": ids}
                            )

                        extracted_text = "\n\n".join(t["text"] for t in structured_turns)
                        file_text = extracted_text if extracted_text else raw_data
                        if not content:
                            content = file_text.strip()
                        else:
                            content = f"{content}\n\n{file_text.strip()}"
                    except Exception as e:
                        return tool_error(f"Failed to read file {file_path}: {e}")

                if not content:
                    return tool_error("Content is empty")
                max_words = int(args.get("max_words") or 80)
                overlap = int(args.get("overlap") or 10)
                ids = self._chunk_and_store(content, max_words, overlap)
                return json.dumps(
                    {"result": "chunked", "count": len(ids), "memory_ids": ids}
                )
            return tool_error(f"Unknown tool: {tool_name}")
        except Exception as e:
            logger.exception("oracle tool %s failed", tool_name)
            return tool_error(str(e))

    def _chunk_text(self, text: str, max_words: int, overlap: int) -> list:
        """Split one text unit into chunks (UTL_TO_CHUNKS in-DB, local fallback)."""
        chunks: list[str] = []
        if self._pool:
            with self._pool.acquire() as conn:
                with conn.cursor() as cur:
                    try:
                        import oracledb

                        # oracledb thin mode returns CLOB chunks as str natively;
                        # do not map to DB_TYPE_LONG (silent 32,760-byte truncation).
                        cur.setinputsizes(oracledb.DB_TYPE_CLOB)

                        cur.execute(
                            """
                            SELECT /*+ NO_PARALLEL */ COLUMN_VALUE FROM TABLE(
                              DBMS_VECTOR.UTL_TO_CHUNKS(
                                TO_CLOB(:1),
                                JSON(:2)
                              )
                            )
                            """,
                            (
                                text,
                                json.dumps(
                                    {
                                        "by": "words",
                                        "max": max_words,
                                        "overlap": overlap,
                                        "split": "recursively",
                                    }
                                ),
                            ),
                        )

                        for row in cur:
                            val = row[0]
                            if not val:
                                continue
                            ctext = str(val)
                            try:
                                payload = json.loads(ctext)
                                ctext = (
                                    payload.get("chunk_data")
                                    or payload.get("chunk_text")
                                    or ctext
                                )
                            except Exception:
                                pass
                            if ctext:
                                chunks.append(str(ctext))
                    except Exception as e:
                        logger.debug("UTL_TO_CHUNKS failed (%s); local split", e)
        if not chunks:
            words = text.split()
            step = max(1, max_words - overlap)
            for i in range(0, len(words), step):
                chunks.append(" ".join(words[i : i + max_words]))
        return chunks

    def _chunk_and_store(
        self,
        content: str,
        max_words: int,
        overlap: int,
        turns: list[dict[str, Any]] | None = None,
    ) -> list:
        """Chunk one text unit or an ordered list of conversation turns.

        Each supplied turn ({"text", "sender", "conv_ref", "turn_id"}) is chunked
        independently and every chunk is stamped with turn_index / chunk_index /
        turn_id / conv_ref, so the source conversation can be reassembled in
        order with `ORDER BY turn_index, chunk_index`. Passing only `content`
        keeps the old single-unit behaviour (one turn, turn_index 0).
        """
        if not self._pool:
            raise RuntimeError(self._init_error or "pool not initialized")
        if turns is None:
            turns = [{"text": content}] if content else []
        ids = []
        rows = []
        for t_index, turn in enumerate(turns):
            text = (turn.get("text") or "").strip()
            if not text:
                continue
            sender = turn.get("sender") or ""
            conv_ref = (turn.get("conv_ref") or "")[:512] or None
            turn_id = (turn.get("turn_id") or f"t{t_index:05d}")[:64]
            for c_index, ch in enumerate(self._chunk_text(text, max_words, overlap)):
                mid = f"chk_{uuid.uuid4().hex[:16]}"
                rows.append({
                    "memory_id": mid,
                    "session_id": self._session_id,
                    "role": "chunk",
                    "content": ch,
                    "target": "memory",
                    "metadata": {
                        "kind": "chunk",
                        "index": t_index,
                        "chunk_index": c_index,
                        "turn_id": turn_id,
                        "conv_ref": conv_ref,
                        "sender": sender or None,
                    },
                    "turn_index": t_index,
                    "chunk_index": c_index,
                    "turn_id": turn_id,
                    "conv_ref": conv_ref,
                })
                ids.append(mid)

        if rows:
            self._insert_many(rows)
        return ids

    def shutdown(self) -> None:
        # Stop accepting new background writes, then drain in-flight and
        # pending work before closing the pool so no turn is lost to a
        # connection closed underneath a running writer.
        self._write_stop.set()
        for t in (self._write_worker, self._embed_worker, self._prefetch_thread):
            if t and t.is_alive():
                t.join(timeout=10.0)
        if self._pool is not None:
            try:
                self._pool.close()
            except Exception as e:
                logger.debug("oracle pool close: %s", e)
            self._pool = None


def register(ctx) -> None:
    provider = OracleMemoryProvider()
    ctx.register_memory_provider(provider)
    if hasattr(ctx, "register_hook"):
        try:
            ctx.register_hook("on_skill_lifecycle", provider.on_skill_lifecycle)
        except Exception as exc:
            logger.debug("Failed to register on_skill_lifecycle hook: %s", exc)
    if hasattr(ctx, "register_skill"):
        try:
            skill_path = Path(__file__).parent / "skills" / "oracle-import" / "SKILL.md"
            if skill_path.exists():
                ctx.register_skill(
                    "oracle-import",
                    skill_path,
                    "Import conversation archives and documents into Oracle 26ai ADB memory without LLM rate limits",
                )
        except Exception as exc:
            logger.debug("Failed to register oracle-import skill: %s", exc)
