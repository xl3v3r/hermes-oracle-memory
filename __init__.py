"""Oracle 26ai memory plugin — MemoryProvider for Autonomous AI Database.

VECTOR IVF + Oracle Text + JSON search as the primary Hermes memory backend.
Credentials: OCI_DB_USER / OCI_DB_PASSWORD / OCI_DB_DSN in $HERMES_HOME/.env.
Embeddings: Option B (768-d in-database ONNX model) as primary, with Option C
(OpenRouter qwen/qwen3-embedding-4b at 768-d FLOAT64) as automatic graceful fallback.
OCI GenAI cohere.embed-v4.0 is tried first if configured; on failure it falls back.
"""

from __future__ import annotations

import array
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any
from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt
from agent.secret_scope import get_secret
from tools.registry import tool_error

logger = logging.getLogger(__name__)

_PREFETCH_WAIT_SECS = 0.5
_EMBED_DIM = 768
_EMBED_MODEL = "qwen/qwen3-embedding-4b"
_OCI_EMBED_MODEL = "cohere.embed-v4.0"
_EMBED_CACHE_MAX = 256
_EMBED_BATCH = 32
_OCI_RETRY_AFTER_SECS = 300.0
_ONNX_RETRY_AFTER_SECS = 300.0
_FETCH_ARRAYSIZE = 64
_CONTAINS_CLEAN_RE = re.compile(r"[^\w\s-]")
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
            "Search Oracle 26ai persistent memory (VECTOR cosine + Oracle Text + JSON). "
            "Use before answering questions that depend on prior facts, preferences, "
            "decisions, people, or projects. mode=hybrid (default) ranks both vector "
            "and keyword; vector = semantic only; text = CONTAINS only."
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
            },
            "required": ["query"],
        },
    },
    {
        "name": "oracle_add",
        "description": (
            "Store a durable fact in Oracle 26ai (embedded + Text + JSON indexed). "
            "Call when the user states a lasting preference, correction, decision, "
            "or personal detail. Skip chit-chat and facts already stored."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The fact to store."},
                "target": {
                    "type": "string",
                    "enum": ["memory", "user"],
                    "description": "memory = environment/conventions; user = who they are.",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "oracle_update",
        "description": "Replace the text of an existing memory by memory_id from oracle_search.",
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
        "description": "Delete a memory by memory_id from oracle_search.",
        "parameters": {
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
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
    "Oracle AI Database 26ai is the primary persistent memory. "
    "Call oracle_search before answering anything that could depend on prior context "
    "(preferences, facts, history, people, projects, decisions). "
    "Use oracle_add the moment a lasting fact is stated. "
    "oracle_update / oracle_delete manage by memory_id. "
    "oracle_chunk splits long documents via in-database UTL_TO_CHUNKS."
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
        self._embed_cache: OrderedDict[str, array.array] = OrderedDict()
        self._embed_cache_lock = threading.Lock()
        self._embed_pending: deque[tuple[str, str]] = deque()
        self._embed_worker: threading.Thread | None = None
        self._oci_embed_failed_at: float | None = None
        self._oci_client: Any | None = None
        self._oci_signer: Any | None = None
        self._onnx_failed_at: float | None = None
        self._lock = threading.Lock()
        self._sync_threads: set[threading.Thread] = set()
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
                "max": 8,
                "increment": 1,
                "threaded": True,
                "getmode": oracledb.POOL_GETMODE_WAIT,
                "timeout": 5,
                "wait_timeout": 5,
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

            self._pool = oracledb.SessionPool(**pool_kwargs)
            self._ensure_schema()
            logger.info("Oracle 26ai memory pool ready (session=%s)", self._session_id)
        except Exception as e:
            self._init_error = str(e)
            logger.error("Oracle memory pool failed: %s", e)
            self._pool = None

    def _ensure_schema(self) -> None:
        if not self._pool:
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
        ]
        with self._pool.acquire() as conn, conn.cursor() as cur:
            for sql in stmts:
                try:
                    cur.execute(sql, [self._table])
                except Exception as e:
                    if "ORA-01430" not in str(e):  # column already exists
                        logger.debug("schema stmt skipped: %s", e)

    def system_prompt_block(self) -> str:
        if not self._pool:
            reason = self._init_error or self.unavailable_reason() or "not initialized"
            return f"# Oracle 26ai Memory\nUnavailable ({reason}). Built-in MEMORY.md still applies."
        return (
            "# Oracle 26ai Memory\n"
            f"Active (primary). ADB VECTOR IVF + Oracle Text + JSON. Agent: {self._agent_id}.\n"
            f"{_PROMPT_BODY}"
        )

    # -- embeddings ----------------------------------------------------------

    def _cache_get(self, text: str) -> array.array | None:
        key = text[:8000]
        with self._embed_cache_lock:
            vec = self._embed_cache.get(key)
            if vec is not None:
                self._embed_cache.move_to_end(key)
            return vec

    def _cache_put(self, text: str, vec: array.array) -> None:
        key = text[:8000]
        with self._embed_cache_lock:
            self._embed_cache[key] = vec
            self._embed_cache.move_to_end(key)
            while len(self._embed_cache) > _EMBED_CACHE_MAX:
                self._embed_cache.popitem(last=False)

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
            logger.info("OCI GenAI embed unavailable (%s); using OpenRouter", e)
            with self._embed_lock:
                self._oci_embed_failed_at = now
            return None

    def _embed_openrouter(self, texts: list[str]) -> list[array.array | None]:
        out: list[array.array | None] = [None] * len(texts)
        if not texts:
            return out
        try:
            key = get_secret("OPENROUTER_API_KEY")
        except Exception:
            # No profile secret scope on this worker thread (multiplex). OCI is the
            # preferred path anyway; fail soft and let the caller keep the None vec.
            logger.debug("OpenRouter embed skipped: no OPENROUTER_API_KEY in scope")
            return out
        if not key or not isinstance(key, str):
            return out

        # Filter and track non-empty texts to avoid OpenRouter 400 Bad Request on empty strings
        # or TypeError on None.
        valid_indices: list[int] = [
            i
            for i, t in enumerate(texts)
            if t is not None and isinstance(t, str) and t.strip()
        ]
        if not valid_indices:
            return out

        input_payload = [texts[i][:8000] for i in valid_indices]

        try:
            import urllib.error
            import urllib.request

            req_data = json.dumps(
                {
                    "model": _EMBED_MODEL,
                    "input": input_payload,
                    "dimensions": _EMBED_DIM,
                }
            ).encode("utf-8")

            max_retries = 3
            backoff = 1.5
            data = None

            for attempt in range(1, max_retries + 1):
                try:
                    req = urllib.request.Request(
                        "https://openrouter.ai/api/v1/embeddings",
                        data=req_data,
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    break
                except urllib.error.HTTPError as he:
                    if (he.code == 429 or he.code >= 500) and attempt < max_retries:
                        time.sleep(backoff)
                        backoff *= 2.0
                        continue
                    raise
                except Exception as ex:
                    if attempt < max_retries:
                        time.sleep(backoff)
                        backoff *= 2.0
                        continue
                    raise

            if data:
                for item in data.get("data") or []:
                    resp_idx = int(item.get("index", 0))
                    vec = item.get("embedding") or []
                    if 0 <= resp_idx < len(valid_indices) and len(vec) == _EMBED_DIM:
                        orig_idx = valid_indices[resp_idx]
                        out[orig_idx] = array.array("f", vec)
        except Exception as e:
            logger.warning("OpenRouter embed failed: %s", e)
        return out

    def _embed_many(self, texts: list[str]) -> list[array.array | None]:
        if not texts:
            return []
        cached: list[array.array | None] = [
            self._cache_get(t) if t else None for t in texts
        ]
        miss_idx = [i for i, (t, v) in enumerate(zip(texts, cached)) if t and v is None]
        if not miss_idx:
            return cached
        unique_miss_texts: list[str] = []
        unique_indices: dict[str, list[int]] = {}
        for i in miss_idx:
            text = texts[i]
            if text not in unique_indices:
                unique_indices[text] = []
                unique_miss_texts.append(text)
            unique_indices[text].append(i)
        miss_texts = unique_miss_texts
        got: list[array.array | None] | None = None

        # Option B: Primary in-database ONNX model embedding
        onnx_enabled = bool(
            self._onnx_model
            and self._pool
            and (
                self._onnx_failed_at is None
                or time.monotonic() - self._onnx_failed_at >= _ONNX_RETRY_AFTER_SECS
            )
        )
        if onnx_enabled:
            try:
                db_vecs: list[array.array | None] = []
                with self._pool.acquire() as conn, conn.cursor() as cur:
                    for t in miss_texts:
                        cur.execute(
                            f"SELECT VECTOR_EMBEDDING({self._onnx_model} USING :1 AS DATA) FROM DUAL",
                            [t[:4000]],
                        )
                        row = cur.fetchone()
                        vec = row[0] if row else None
                        if vec is not None and len(vec) == _EMBED_DIM:
                            db_vecs.append(vec)
                        else:
                            db_vecs.append(None)
                if any(v is not None for v in db_vecs):
                    got = db_vecs
                self._onnx_failed_at = None
            except Exception as e:
                logger.warning(
                    "In-database ONNX embed_many failed (%s); falling back to OpenRouter",
                    e,
                )
                self._onnx_failed_at = time.monotonic()
                got = None

        # Option C: Fallback to OCI GenAI or OpenRouter
        if got is None:
            got = self._embed_external(miss_texts)

        for text, vec in zip(miss_texts, got):
            if vec is not None:
                self._cache_put(text, vec)
            for i in unique_indices[text]:
                cached[i] = vec
        return cached

    def _embed_external(self, texts: list[str]) -> list[array.array | None]:
        """Embed through remote providers without retrying the in-database model."""
        got = self._embed_oci(texts)
        return got if got is not None else self._embed_openrouter(texts)

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
        def run() -> None:
            try:
                self._insert(
                    memory_id=memory_id,
                    session_id=session_id,
                    role=role,
                    content=content,
                    target=target,
                    metadata=metadata,
                )
            except Exception as e:
                logger.warning("oracle background insert failed: %s", e)
            finally:
                with self._lock:
                    self._sync_threads.discard(thread)

        thread = threading.Thread(target=run, daemon=True, name="oracle-sync")
        with self._lock:
            self._sync_threads.add(thread)
        thread.start()

    def _embed_worker_loop(self) -> None:
        while True:
            with self._embed_lock:
                if not self._embed_pending:
                    self._embed_worker = None
                    return
                batch = [self._embed_pending.popleft() for _ in range(min(_EMBED_BATCH, len(self._embed_pending)))]
            ids = [m for m, _ in batch]
            texts = [c for _, c in batch]
            try:
                vecs = self._embed_many(texts)
                if not self._pool:
                    continue
                with self._pool.acquire() as conn:
                    with conn.cursor() as cur:
                        # Optimize: Use executemany for bulk updating embeddings in a single round-trip
                        sql = f"UPDATE {self._table} SET embedding = :1 WHERE memory_id = :2"  # nosec B608
                        binds = [(vec, mid) for mid, vec in zip(ids, vecs) if vec is not None]
                        if binds:
                            cur.executemany(sql, binds)
                    conn.commit()
            except Exception as e:
                logger.debug("embed backfill batch failed: %s", e)

    def _contains_query(self, query: str) -> str:
        cleaned = _CONTAINS_CLEAN_RE.sub(" ", query or "")
        tokens = [t for t in cleaned.split() if len(t) > 2][:8]
        if not tokens:
            return "memory"
        # ACCUM ranks a union of terms; AND of every token misses too often.
        return " ACCUM ".join(tokens)

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
        meta = json.dumps(metadata or {})
        txt = (content or "")[:4000]
        onnx_enabled = bool(
            self._onnx_model
            and (
                self._onnx_failed_at is None
                or time.monotonic() - self._onnx_failed_at >= _ONNX_RETRY_AFTER_SECS
            )
        )
        if onnx_enabled:
            sql = f"""
                INSERT INTO {self._table}
                    (memory_id, session_id, agent_id, role, content, embedding,
                     created_at, target, metadata, content_txt)
                VALUES (:1, :2, :3, :4, :5, VECTOR_EMBEDDING({self._onnx_model} USING :6 AS DATA), SYSTIMESTAMP, :7, :8, :9)
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
            )
            try:
                with self._pool.acquire() as conn, conn.cursor() as cur:
                    cur.execute(sql, binds)
                    conn.commit()
                return
            except Exception as e:
                logger.warning(
                    "In-database ONNX insert failed (%s); falling back to OpenRouter embedding",
                    e,
                )
                self._onnx_failed_at = time.monotonic()
                if vec is None:
                    vec = self._embed_external([content])[0]

        sql = f"""
            INSERT INTO {self._table}
                (memory_id, session_id, agent_id, role, content, embedding,
                 created_at, target, metadata, content_txt)
            VALUES (:1, :2, :3, :4, :5, :6, SYSTIMESTAMP, :7, :8, :9)
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

        onnx_enabled = bool(
            self._onnx_model
            and (
                self._onnx_failed_at is None
                or time.monotonic() - self._onnx_failed_at >= _ONNX_RETRY_AFTER_SECS
            )
        )
        if onnx_enabled:
            sql = f"""
                INSERT INTO {self._table}
                    (memory_id, session_id, agent_id, role, content, embedding,
                     created_at, target, metadata, content_txt)
                VALUES (:1, :2, :3, :4, :5, VECTOR_EMBEDDING({self._onnx_model} USING :6 AS DATA), SYSTIMESTAMP, :7, :8, :9)
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
                )
                binds_list.append(binds)
            try:
                with self._pool.acquire() as conn, conn.cursor() as cur:
                    cur.executemany(sql, binds_list)
                    conn.commit()
                return
            except Exception as e:
                logger.warning(
                    "In-database ONNX insert_many failed (%s); falling back to OpenRouter embedding",
                    e,
                )
                self._onnx_failed_at = time.monotonic()

        binds_list = []
        backfill_queue = []
        contents = [(r.get("content") or "") for r in rows]
        needed_indices = [i for i, r in enumerate(rows) if r.get("embedding") is None]
        needed_texts = [contents[i] for i in needed_indices]
        fetched_vecs = self._embed_many(needed_texts) if needed_texts else []
        vec_map = {idx: fetched_vecs[j] for j, idx in enumerate(needed_indices)}

        for i, row in enumerate(rows):
            memory_id = row.get("memory_id")
            session_id = row.get("session_id") or self._session_id or "none"
            role = row.get("role")
            content = row.get("content")
            target = row.get("target")
            metadata = row.get("metadata")
            embedding = row.get("embedding") if row.get("embedding") is not None else vec_map.get(i)

            vec = embedding if embedding is not None else None
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
            )
            binds_list.append(binds)

            if vec is None:
                backfill_queue.append((memory_id, content))

        sql = f"""
            INSERT INTO {self._table}
                (memory_id, session_id, agent_id, role, content, embedding,
                 created_at, target, metadata, content_txt)
            VALUES (:1, :2, :3, :4, :5, :6, SYSTIMESTAMP, :7, :8, :9)
        """

        with self._pool.acquire() as conn, conn.cursor() as cur:
            cur.executemany(sql, binds_list)
            conn.commit()

        for memory_id, content in backfill_queue:
            self._queue_embed_backfill(memory_id, content)

    _JUNK = (
        " AND memory_id NOT LIKE 'test_%'"
        " AND NVL(session_id, 'x') <> 'diagnostic-session-001' "
    )

    def _fetch_rows(self, cur, sql: str, binds: dict) -> list:
        import oracledb

        # ⚡ Bolt: Use outputtypehandler to fetch CLOBs directly as strings.
        # This prevents the N+1 network round-trips caused by calling .read()
        # on LOB locators.
        def fetch_lobs_as_strings(cursor, metadata):
            if metadata.type_code is oracledb.DB_TYPE_CLOB:
                return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)

        fetch_k = binds.get("k", _FETCH_ARRAYSIZE)
        cur.arraysize = max(_FETCH_ARRAYSIZE, int(fetch_k or 0))
        cur.prefetchrows = cur.arraysize
        cur.outputtypehandler = fetch_lobs_as_strings
        cur.execute(sql, binds)
        out = []
        for row in cur.fetchall():
            content = row[4]
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

    def _build_text_search_sql(self, contains_q: str, fetch_k: int, tgt_sql: str, tgt: str) -> Tuple[str, dict]:
        sql = f"""
            SELECT memory_id, session_id, agent_id, role, content, target,
                   SCORE(1) AS txt_score, NULL AS distance
            FROM {self._table}
            WHERE CONTAINS(content, :q, 1) > 0
            {self._JUNK}{tgt_sql}
            ORDER BY txt_score DESC
            FETCH FIRST :k ROWS ONLY
        """
        binds = {"q": contains_q, "k": fetch_k}
        if tgt_sql:
            binds["tgt"] = tgt
        return sql, binds

    def _build_vector_search_sql(self, query: str, vec: Any, fetch_k: int, tgt_sql: str, tgt: str, use_onnx: bool = True) -> Tuple[str, dict]:
        if use_onnx and self._onnx_model:
            sql = f"""
                SELECT memory_id, session_id, agent_id, role, content, target,
                       0 AS txt_score,
                       VECTOR_DISTANCE(embedding, VECTOR_EMBEDDING({self._onnx_model} USING :qv AS DATA), COSINE) AS distance
                FROM {self._table}
                WHERE embedding IS NOT NULL
                {self._JUNK}{tgt_sql}
                ORDER BY distance ASC
                FETCH FIRST :k ROWS ONLY
            """
            binds = {"qv": query[:4000], "k": fetch_k}
        else:
            sql = f"""
                SELECT memory_id, session_id, agent_id, role, content, target,
                       0 AS txt_score,
                       VECTOR_DISTANCE(embedding, TO_VECTOR(:v, 768, FLOAT32), COSINE) AS distance
                FROM {self._table}
                WHERE embedding IS NOT NULL
                {self._JUNK}{tgt_sql}
                ORDER BY distance ASC
                FETCH FIRST :k ROWS ONLY
            """
            binds = {"v": vec, "k": fetch_k}

        if tgt_sql:
            binds["tgt"] = tgt
        return sql, binds

    def _build_hybrid_search_sql(self, query: str, vec: Any, contains_q: str, fetch_k: int, tgt_sql: str, tgt: str, use_onnx: bool = True) -> Tuple[str, dict]:
        if use_onnx and self._onnx_model:
            fused_sql = f"""
                SELECT memory_id, session_id, agent_id, role, content, target,
                       txt_score, distance
                FROM (
                    SELECT memory_id, session_id, agent_id, role, content, target,
                           0 AS txt_score,
                           VECTOR_DISTANCE(embedding, VECTOR_EMBEDDING({self._onnx_model} USING :qv AS DATA), COSINE) AS distance
                    FROM {self._table}
                    WHERE embedding IS NOT NULL
                    {self._JUNK}{tgt_sql}
                    ORDER BY distance ASC
                    FETCH FIRST :k ROWS ONLY
                )
                UNION ALL
                SELECT memory_id, session_id, agent_id, role, content, target,
                       txt_score, distance
                FROM (
                    SELECT memory_id, session_id, agent_id, role, content, target,
                           SCORE(1) AS txt_score, NULL AS distance
                    FROM {self._table}
                    WHERE CONTAINS(content, :q, 1) > 0
                    {self._JUNK}{tgt_sql}
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
                SELECT memory_id, session_id, agent_id, role, content, target,
                       txt_score, distance
                FROM (
                    SELECT memory_id, session_id, agent_id, role, content, target,
                           0 AS txt_score,
                           VECTOR_DISTANCE(embedding, TO_VECTOR(:v, 768, FLOAT32), COSINE) AS distance
                    FROM {self._table}
                    WHERE embedding IS NOT NULL
                    {self._JUNK}{tgt_sql}
                    ORDER BY distance ASC
                    FETCH FIRST :k ROWS ONLY
                )
                UNION ALL
                SELECT memory_id, session_id, agent_id, role, content, target,
                       txt_score, distance
                FROM (
                    SELECT memory_id, session_id, agent_id, role, content, target,
                           SCORE(1) AS txt_score, NULL AS distance
                    FROM {self._table}
                    WHERE CONTAINS(content, :q, 1) > 0
                    {self._JUNK}{tgt_sql}
                    ORDER BY txt_score DESC
                    FETCH FIRST :k ROWS ONLY
                )
            """
            fused_binds = {"v": vec, "q": contains_q, "k": fetch_k}
        if tgt_sql:
            fused_binds["tgt"] = tgt
        return fused_sql, fused_binds

    def _execute_hybrid_search(
        self, cur: Any, query: str, vec: Any, contains_q: str, top_k: int, fetch_k: int, tgt_sql: str, tgt: str
    ) -> list:
        merged: dict = {}
        use_onnx = bool(self._onnx_model)
        try:
            fused_sql, fused_binds = self._build_hybrid_search_sql(query, vec, contains_q, fetch_k, tgt_sql, tgt, use_onnx=use_onnx)
            for r in self._fetch_rows(cur, fused_sql, fused_binds):
                prev = merged.get(r["memory_id"])
                if prev:
                    prev["score"] = round(
                        min(1.0, prev["score"] + r["score"] * 0.3), 4
                    )
                else:
                    merged[r["memory_id"]] = r
        except Exception as e:
            if use_onnx:
                logger.warning(
                    "In-database ONNX hybrid search failed (%s); falling back to OpenRouter 768-d embedding",
                    e,
                )
                if vec is None:
                    vec = self._embed_external([query])[0]
                if vec is not None:
                    try:
                        fused_sql, fused_binds = self._build_hybrid_search_sql(query, vec, contains_q, fetch_k, tgt_sql, tgt, use_onnx=False)
                        for r in self._fetch_rows(cur, fused_sql, fused_binds):
                            prev = merged.get(r["memory_id"])
                            if prev:
                                prev["score"] = round(
                                    min(1.0, prev["score"] + r["score"] * 0.3), 4
                                )
                            else:
                                merged[r["memory_id"]] = r
                    except Exception as e2:
                        logger.debug("Fallback hybrid failed (%s); falling back to vector or text", e2)
                        try:
                            v_sql, v_binds = self._build_vector_search_sql(query, vec, fetch_k, tgt_sql, tgt, use_onnx=False)
                            for r in self._fetch_rows(cur, v_sql, v_binds):
                                merged[r["memory_id"]] = r
                        except Exception as e3:
                            logger.debug("Fallback vector also failed (%s); falling back to text-only", e3)
                            t_sql, t_binds = self._build_text_search_sql(contains_q, fetch_k, tgt_sql, tgt)
                            for r in self._fetch_rows(cur, t_sql, t_binds):
                                merged[r["memory_id"]] = r
                else:
                    t_sql, t_binds = self._build_text_search_sql(contains_q, fetch_k, tgt_sql, tgt)
                    for r in self._fetch_rows(cur, t_sql, t_binds):
                        merged[r["memory_id"]] = r
            else:
                logger.debug(
                    "fused hybrid failed (%s); falling back to vector-only", e
                )
                if vec is not None:
                    try:
                        v_sql, v_binds = self._build_vector_search_sql(query, vec, fetch_k, tgt_sql, tgt, use_onnx=False)
                        for r in self._fetch_rows(cur, v_sql, v_binds):
                            merged[r["memory_id"]] = r
                    except Exception as e_vec:
                        logger.debug("vector-only failed (%s); falling back to text", e_vec)
                        t_sql, t_binds = self._build_text_search_sql(contains_q, fetch_k, tgt_sql, tgt)
                        for r in self._fetch_rows(cur, t_sql, t_binds):
                            merged[r["memory_id"]] = r
                else:
                    t_sql, t_binds = self._build_text_search_sql(contains_q, fetch_k, tgt_sql, tgt)
                    for r in self._fetch_rows(cur, t_sql, t_binds):
                        merged[r["memory_id"]] = r
        ranked = sorted(
            merged.values(), key=lambda x: x["score"], reverse=True
        )
        if len(ranked) <= 1:
            return ranked[:top_k]
        return self._rerank(query, ranked, top_k, cur=cur)

    def _search(
        self, query: str, top_k: int = 8, mode: str = "hybrid", target: str = "any"
    ) -> list:
        if not self._pool or not query:
            return []
        top_k = max(1, min(int(top_k or 8), 30))
        mode = (mode or "hybrid").lower()
        tgt = (target or "any").lower()
        tgt_sql = "" if tgt in ("", "any") else " AND target = :tgt "
        contains_q = self._contains_query(query)
        # Oracle can generate the query vector in SQL; avoid a redundant client
        # embedding request while the configured ONNX model is available.
        vec = None if mode == "text" or self._onnx_model else self._embed(query)
        if not self._onnx_model and mode != "text" and vec is None:
            mode = "text"

        fetch_k = top_k * 2 if mode == "hybrid" else top_k
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                if mode == "text":
                    sql, binds = self._build_text_search_sql(contains_q, fetch_k, tgt_sql, tgt)
                    rows = self._fetch_rows(cur, sql, binds)
                elif mode == "vector":
                    rows = []
                    use_onnx = bool(self._onnx_model)
                    if use_onnx:
                        try:
                            sql, binds = self._build_vector_search_sql(query, vec, fetch_k, tgt_sql, tgt, use_onnx=True)
                            rows = self._fetch_rows(cur, sql, binds)
                        except Exception as e:
                            logger.warning(
                                "In-database ONNX vector search failed (%s); falling back to OpenRouter 768-d embedding",
                                e,
                            )
                            if vec is None:
                                vec = self._embed_external([query])[0]
                            if vec is not None:
                                try:
                                    sql, binds = self._build_vector_search_sql(query, vec, fetch_k, tgt_sql, tgt, use_onnx=False)
                                    rows = self._fetch_rows(cur, sql, binds)
                                except Exception as e2:
                                    logger.warning(
                                        "Fallback vector search failed (%s); falling back to text search",
                                        e2,
                                    )
                                    sql, binds = self._build_text_search_sql(contains_q, fetch_k, tgt_sql, tgt)
                                    rows = self._fetch_rows(cur, sql, binds)
                            else:
                                sql, binds = self._build_text_search_sql(contains_q, fetch_k, tgt_sql, tgt)
                                rows = self._fetch_rows(cur, sql, binds)
                    else:
                        if vec is not None:
                            try:
                                sql, binds = self._build_vector_search_sql(query, vec, fetch_k, tgt_sql, tgt, use_onnx=False)
                                rows = self._fetch_rows(cur, sql, binds)
                            except Exception as e:
                                logger.warning(
                                    "Vector search failed (%s); falling back to text search",
                                    e,
                                )
                                sql, binds = self._build_text_search_sql(contains_q, fetch_k, tgt_sql, tgt)
                                rows = self._fetch_rows(cur, sql, binds)
                        else:
                            sql, binds = self._build_text_search_sql(contains_q, fetch_k, tgt_sql, tgt)
                            rows = self._fetch_rows(cur, sql, binds)
                else:
                    rows = self._execute_hybrid_search(cur, query, vec, contains_q, top_k, fetch_k, tgt_sql, tgt)
        return rows[:top_k]

    def _rerank(self, query: str, rows: list, top_k: int, cur=None) -> list:
        """Best-effort DBMS_VECTOR.RERANK; identity if the call is unavailable."""
        if not rows:
            return rows
        docs = []
        for r in rows[: min(20, len(rows))]:
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
            body = (r.get("content") or "").replace("\n", " ").strip()
            if len(body) > 280:
                body = body[:277] + "..."
            lines.append(
                f"- [{r.get('target', 'memory')}|{r.get('agent_id', 'hermes')}|{r.get('score', 0)}] {body}"
            )
        return "## Oracle 26ai Memory\n" + "\n".join(lines)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        self._start_prefetch(query)

    def _start_prefetch(self, query: str) -> None:
        if not isinstance(query, str) or not query or not self._pool or is_trivial_prompt(query):
            return

        def _run():
            try:
                rows = self._search(query, top_k=6, mode="hybrid")
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

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return list(TOOL_SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        if not self._pool:
            return json.dumps(
                {
                    "error": f"Oracle memory not initialized: {self._init_error or self.unavailable_reason()}"
                }
            )
        try:
            if tool_name == "oracle_search":
                rows = self._search(
                    args.get("query") or "",
                    top_k=int(args.get("top_k") or 8),
                    mode=args.get("mode") or "hybrid",
                    target=args.get("target") or "any",
                )
                if not rows:
                    return json.dumps(
                        {"result": "No relevant memories found.", "count": 0}
                    )
                return json.dumps({"results": rows, "count": len(rows)})
            if tool_name == "oracle_add":
                content = (args.get("content") or "").strip()
                if not content:
                    return tool_error("Missing required parameter: content")
                mid = f"fact_{uuid.uuid4().hex[:16]}"
                self._insert(
                    memory_id=mid,
                    session_id=self._session_id,
                    role="fact",
                    content=content,
                    target=args.get("target") or "memory",
                    metadata={"kind": "tool"},
                )
                return json.dumps({"result": "Fact stored.", "memory_id": mid})
            if tool_name == "oracle_update":
                mid = args.get("memory_id")
                content = (args.get("content") or "").strip()
                if not mid or not content:
                    return tool_error("memory_id and content required")
                vec = None if self._onnx_model else self._embed(content)
                onnx_enabled = bool(
                    self._onnx_model
                    and (
                        self._onnx_failed_at is None
                        or time.monotonic() - self._onnx_failed_at >= _ONNX_RETRY_AFTER_SECS
                    )
                )
                txt = content[:4000]
                with self._pool.acquire() as conn:
                    with conn.cursor() as cur:
                        if onnx_enabled:
                            try:
                                sql = f"""
                                    UPDATE {self._table}
                                    SET content = :1, embedding = VECTOR_EMBEDDING({self._onnx_model} USING :2 AS DATA), content_txt = :3
                                    WHERE memory_id = :4
                                    """  # nosec B608
                                cur.execute(
                                    sql,
                                    (content, txt, txt, mid),
                                )
                            except Exception as e:
                                logger.warning(
                                    "In-database ONNX update failed (%s); falling back to OpenRouter embedding",
                                    e,
                                )
                                self._onnx_failed_at = time.monotonic()
                                if vec is None:
                                    vec = self._embed_external([content])[0]
                                sql = f"""
                                    UPDATE {self._table}
                                    SET content = :1, embedding = :2, content_txt = :3
                                    WHERE memory_id = :4
                                    """  # nosec B608
                                cur.execute(
                                    sql,
                                    (content, vec, txt, mid),
                                )
                        else:
                            sql = f"""
                                UPDATE {self._table}
                                SET content = :1, embedding = :2, content_txt = :3
                                WHERE memory_id = :4
                                """  # nosec B608
                            cur.execute(
                                sql,
                                (content, vec, txt, mid),
                            )
                        n = cur.rowcount
                    conn.commit()
                if not n:
                    return tool_error(f"Memory not found: {mid}")
                if vec is None:
                    self._queue_embed_backfill(mid, content)
                return json.dumps({"result": "updated", "memory_id": mid})
            if tool_name == "oracle_delete":
                mid = args.get("memory_id")
                if not mid:
                    return tool_error("memory_id required")
                with self._pool.acquire() as conn:
                    with conn.cursor() as cur:
                        query = f"DELETE FROM {self._table} WHERE memory_id = :1"  # nosec B608
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
                        raw_data = p.read_text(encoding="utf-8", errors="replace")
                        extracted_text = ""
                        try:
                            parsed_json = json.loads(raw_data)
                            turns = []
                            if isinstance(parsed_json, dict) and "conversations" in parsed_json:
                                for item in parsed_json["conversations"]:
                                    if isinstance(item, dict):
                                        val = _extract_turn_text(item)
                                        sender = item.get("from") or item.get("role") or ""
                                        if val:
                                            prefix = f"[{sender.upper()}]: " if sender else ""
                                            turns.append(f"{prefix}{val}")
                                    elif isinstance(item, str) and item.strip():
                                        turns.append(item.strip())
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
                                            turns.append("\n\n".join(header_parts))

                                        if "conversations" in item and isinstance(item["conversations"], list):
                                            for sub in item["conversations"]:
                                                if isinstance(sub, dict):
                                                    val = _extract_turn_text(sub)
                                                    sender = sub.get("from") or sub.get("role") or ""
                                                    if val:
                                                        prefix = f"[{sender.upper()}]: " if sender else ""
                                                        turns.append(f"{prefix}{val}")
                                                elif isinstance(sub, str) and sub.strip():
                                                    turns.append(sub.strip())
                                        elif "chat_messages" in item and isinstance(item["chat_messages"], list):
                                            for sub in item["chat_messages"]:
                                                if isinstance(sub, dict):
                                                    val = _extract_turn_text(sub)
                                                    sender = sub.get("sender") or sub.get("from") or sub.get("role") or ""
                                                    if val:
                                                        prefix = f"[{sender.upper()}]: " if sender else ""
                                                        turns.append(f"{prefix}{val}")
                                                elif isinstance(sub, str) and sub.strip():
                                                    turns.append(sub.strip())
                                        else:
                                            val = _extract_turn_text(item)
                                            sender = item.get("from") or item.get("role") or ""
                                            if val:
                                                prefix = f"[{sender.upper()}]: " if sender else ""
                                                turns.append(f"{prefix}{val}")
                                    elif isinstance(item, str) and item.strip():
                                        turns.append(item.strip())
                            if turns:
                                extracted_text = "\n\n".join(turns)
                        except Exception:
                            extracted_text = ""

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

    def _chunk_and_store(self, content: str, max_words: int, overlap: int) -> list:
        if not self._pool:
            raise RuntimeError(self._init_error or "pool not initialized")
        chunks: list[str] = []
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                try:
                    import oracledb

                    # ⚡ Bolt: Use outputtypehandler to fetch CLOB chunks directly as strings
                    def fetch_lobs_as_strings(cursor, metadata):
                        if metadata.type_code is oracledb.DB_TYPE_CLOB:
                            return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)
                    cur.outputtypehandler = fetch_lobs_as_strings
                    cur.setinputsizes(oracledb.DB_TYPE_CLOB)

                    cur.execute(
                        """
                        SELECT COLUMN_VALUE FROM TABLE(
                          DBMS_VECTOR.UTL_TO_CHUNKS(
                            TO_CLOB(:1),
                            JSON(:2)
                          )
                        )
                        """,
                        (
                            content,
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
                        text = str(val)
                        try:
                            payload = json.loads(text)
                            text = (
                                payload.get("chunk_data")
                                or payload.get("chunk_text")
                                or text
                            )
                        except Exception:
                            pass
                        if text:
                            chunks.append(str(text))
                except Exception as e:
                    logger.debug("UTL_TO_CHUNKS failed (%s); local split", e)
        if not chunks:
            words = content.split()
            step = max(1, max_words - overlap)
            for i in range(0, len(words), step):
                chunks.append(" ".join(words[i : i + max_words]))
        ids = []
        rows = []
        for i, ch in enumerate(chunks):
            mid = f"chk_{uuid.uuid4().hex[:16]}"
            rows.append({
                "memory_id": mid,
                "session_id": self._session_id,
                "role": "chunk",
                "content": ch,
                "target": "memory",
                "metadata": {"kind": "chunk", "index": i},
            })
            ids.append(mid)

        self._insert_many(rows)
        return ids

    def shutdown(self) -> None:
        with self._lock:
            sync_threads = list(self._sync_threads)
        for t in (self._prefetch_thread, self._embed_worker, *sync_threads):
            if t and t.is_alive():
                t.join(timeout=3.0)
        if self._pool is not None:
            try:
                self._pool.close()
            except Exception as e:
                logger.debug("oracle pool close: %s", e)
            self._pool = None


def register(ctx) -> None:
    ctx.register_memory_provider(OracleMemoryProvider())
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
