"""Oracle 26ai memory plugin — MemoryProvider for Autonomous AI Database.

VECTOR IVF + Oracle Text + JSON search as the primary Hermes memory backend.
Credentials: OCI_DB_USER / OCI_DB_PASSWORD / OCI_DB_DSN in $HERMES_HOME/.env.
Embeddings: OpenRouter openai/text-embedding-3-small (1536-d FLOAT64) by default.
OCI GenAI cohere.embed-v4.0 (1536-d) is tried first via instance principal; on
404/auth failure this process falls back to OpenRouter and does not retry OCI.
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
from typing import Any, Dict, List, Optional, Tuple

from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt
from agent.secret_scope import get_secret
from tools.registry import tool_error

logger = logging.getLogger(__name__)

_PREFETCH_WAIT_SECS = 0.5
_EMBED_DIM = 1536
_EMBED_MODEL = "openai/text-embedding-3-small"
_OCI_EMBED_MODEL = "cohere.embed-v4.0"
_EMBED_CACHE_MAX = 256
_EMBED_BATCH = 32
_OCI_RETRY_AFTER_SECS = 300.0
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
            "Split long text with in-database DBMS_VECTOR.UTL_TO_CHUNKS, then store "
            "each chunk as its own VECTOR row. Use for documents, logs, or notes "
            "too large for a single fact."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Source text to chunk and store.",
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
            "required": ["content"],
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
    return bool(_INTERNAL_GATEWAY_TURN_RE.match(text or ""))


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
        self._sync_thread: threading.Thread | None = None
        self._embed_lock = threading.Lock()
        self._embed_cache: OrderedDict[str, array.array] = OrderedDict()
        self._embed_cache_lock = threading.Lock()
        self._embed_pending: deque[tuple[str, str]] = deque()
        self._embed_worker: threading.Thread | None = None
        self._oci_embed_failed_at: float | None = None
        self._lock = threading.Lock()
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
        return bool(
            get_secret("OCI_DB_USER")
            and get_secret("OCI_DB_PASSWORD")
            and get_secret("OCI_DB_DSN")
        )

    def unavailable_reason(self) -> str:
        try:
            import oracledb  # noqa: F401
        except Exception:
            return "oracledb is not installed in the Hermes venv"
        missing = [
            k
            for k in ("OCI_DB_USER", "OCI_DB_PASSWORD", "OCI_DB_DSN")
            if not get_secret(k)
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
        self._session_id = session_id or ""
        self._agent_id = kwargs.get("agent_identity") or "hermes"
        self._agent_context = kwargs.get("agent_context") or "primary"

        # Load table name dynamically for OpenClaw shared memory compatibility
        import re as _re

        raw_table = get_secret("HERMES_ORACLE_TABLE") or "hermes_agent_memory"
        self._table = _re.sub(r"[^a-zA-Z0-9_]", "", raw_table)
        self._onnx_model = _re.sub(
            r"[^a-zA-Z0-9_]", "", get_secret("HERMES_ORACLE_ONNX_MODEL") or ""
        )

        self._user = get_secret("OCI_DB_USER", "ADMIN") or "ADMIN"
        password = get_secret("OCI_DB_PASSWORD")
        self._dsn = get_secret("OCI_DB_DSN") or ""
        if not password or not self._dsn:
            self._init_error = "OCI_DB_PASSWORD or OCI_DB_DSN missing"
            logger.error("Oracle memory: %s", self._init_error)
            return
        try:
            import oracledb

            self._pool = oracledb.SessionPool(
                user=self._user,
                password=password,
                dsn=self._dsn,
                min=2,
                max=8,
                increment=1,
                threaded=True,
                getmode=oracledb.POOL_GETMODE_WAIT,
                timeout=5,
                wait_timeout=5,
            )
            self._ensure_schema()
            logger.info("Oracle 26ai memory pool ready (session=%s)", self._session_id)
        except Exception as e:
            self._init_error = str(e)
            logger.error("Oracle memory pool failed: %s", e)
            self._pool = None

    def _ensure_schema(self) -> None:
        if not self._pool:
            return
        stmts = [
            f"""
            CREATE TABLE {self._table} (
                memory_id VARCHAR2(128) PRIMARY KEY,
                session_id VARCHAR2(128),
                agent_id VARCHAR2(64),
                role VARCHAR2(64),
                content CLOB,
                embedding VECTOR,
                created_at TIMESTAMP DEFAULT SYSTIMESTAMP,
                target VARCHAR2(32) DEFAULT 'memory',
                metadata JSON,
                content_txt VARCHAR2(4000)
            )
            """,
            f"CREATE SEARCH INDEX {self._table}_txt_idx ON {self._table}(content) FOR JSON",
            f"ALTER TABLE {self._table} ADD (created_at TIMESTAMP DEFAULT SYSTIMESTAMP)",
            f"ALTER TABLE {self._table} ADD (target VARCHAR2(32) DEFAULT 'memory')",
            f"ALTER TABLE {self._table} ADD (metadata JSON)",
            f"ALTER TABLE {self._table} ADD (content_txt VARCHAR2(4000))",
        ]
        with self._pool.acquire() as conn, conn.cursor() as cur:
            for sql in stmts:
                try:
                    cur.execute(sql)
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
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            region = (
                os.environ.get("OCI_GENAI_REGION")
                or getattr(signer, "region", None)
                or "us-ashburn-1"
            )
            endpoint = f"https://inference.generativeai.{region}.oci.oraclecloud.com"
            client = oci.generative_ai_inference.GenerativeAiInferenceClient(
                config={}, signer=signer, service_endpoint=endpoint
            )
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
                    out.append(array.array("d", vec))
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
        if not key:
            return out
        try:
            import urllib.request

            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/embeddings",
                data=json.dumps(
                    {"model": _EMBED_MODEL, "input": [t[:8000] for t in texts]}
                ).encode(),
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
            for item in data.get("data") or []:
                idx = int(item.get("index", 0))
                vec = item.get("embedding") or []
                if 0 <= idx < len(out) and len(vec) == _EMBED_DIM:
                    out[idx] = array.array("d", vec)
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
        miss_texts = [texts[i] for i in miss_idx]
        got = self._embed_oci(miss_texts)
        if got is None:
            got = self._embed_openrouter(miss_texts)
        for i, vec in zip(miss_idx, got):
            cached[i] = vec
            if vec is not None:
                self._cache_put(texts[i], vec)
        return cached

    def _embed(self, text: str) -> array.array | None:
        if self._onnx_model:
            return None
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
                        binds = [(vec, mid) for mid, vec in zip(ids, vecs) if vec is not None]
                        if binds:
                            cur.executemany(
                                f"UPDATE {self._table} SET embedding = :1 WHERE memory_id = :2",
                                binds,
                            )
                    conn.commit()
            except Exception as e:
                logger.debug("embed backfill batch failed: %s", e)

    def _contains_query(self, query: str) -> str:
        cleaned = re.sub(r"[^\w\s-]", " ", query or "")
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
        if self._onnx_model:
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
        else:
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

        if vec is None and not self._onnx_model:
            self._queue_embed_backfill(memory_id, content)

    _JUNK = (
        " AND memory_id NOT LIKE 'test_%'"
        " AND NVL(session_id, 'x') <> 'diagnostic-session-001' "
    )

    def _fetch_rows(self, cur, sql: str, binds: dict) -> list:
        import oracledb

        cur.execute(sql, binds)
        out = []
        for row in cur:
            content = row[4].read() if isinstance(row[4], oracledb.LOB) else row[4]
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
        vec = None if mode == "text" else self._embed(query)
        # In ONNX mode, we always have a vector implicitly.
        if self._onnx_model and mode != "text":
            # We don't check `vec is None` because we embed in DB
            pass
        elif mode != "text" and vec is None:
            mode = "text"

        fetch_k = top_k * 2 if mode == "hybrid" else top_k
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                if mode == "text":
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
                    rows = self._fetch_rows(cur, sql, binds)
                elif mode == "vector":
                    if self._onnx_model:
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
                                   VECTOR_DISTANCE(embedding, :v, COSINE) AS distance
                            FROM {self._table}
                            WHERE embedding IS NOT NULL
                            {self._JUNK}{tgt_sql}
                            ORDER BY distance ASC
                            FETCH FIRST :k ROWS ONLY
                        """
                        binds = {"v": vec, "k": fetch_k}

                    if tgt_sql:
                        binds["tgt"] = tgt
                    rows = self._fetch_rows(cur, sql, binds)
                else:
                    if self._onnx_model:
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
                        fused_binds = {
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
                                       VECTOR_DISTANCE(embedding, :v, COSINE) AS distance
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
                    merged: dict = {}
                    try:
                        for r in self._fetch_rows(cur, fused_sql, fused_binds):
                            prev = merged.get(r["memory_id"])
                            if prev:
                                prev["score"] = round(
                                    min(1.0, prev["score"] + r["score"] * 0.3), 4
                                )
                            else:
                                merged[r["memory_id"]] = r
                    except Exception as e:
                        logger.debug(
                            "fused hybrid failed (%s); falling back to vector-only", e
                        )
                        if self._onnx_model:
                            v_sql = f"""
                                SELECT memory_id, session_id, agent_id, role, content, target,
                                       0 AS txt_score,
                                       VECTOR_DISTANCE(embedding, VECTOR_EMBEDDING({self._onnx_model} USING :qv AS DATA), COSINE) AS distance
                                FROM {self._table}
                                WHERE embedding IS NOT NULL
                                {self._JUNK}{tgt_sql}
                                ORDER BY distance ASC
                                FETCH FIRST :k ROWS ONLY
                            """
                            v_binds = {"qv": query[:4000], "k": fetch_k}
                        else:
                            v_sql = f"""
                                SELECT memory_id, session_id, agent_id, role, content, target,
                                       0 AS txt_score,
                                       VECTOR_DISTANCE(embedding, :v, COSINE) AS distance
                                FROM {self._table}
                                WHERE embedding IS NOT NULL
                                {self._JUNK}{tgt_sql}
                                ORDER BY distance ASC
                                FETCH FIRST :k ROWS ONLY
                            """
                            v_binds = {"v": vec, "k": fetch_k}

                        if tgt_sql:
                            v_binds["tgt"] = tgt
                        for r in self._fetch_rows(cur, v_sql, v_binds):
                            merged[r["memory_id"]] = r
                    ranked = sorted(
                        merged.values(), key=lambda x: x["score"], reverse=True
                    )
                    rows = self._rerank(query, ranked, top_k, cur=cur)
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
        if not query or not self._pool or is_trivial_prompt(query):
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
        if not query or is_trivial_prompt(query):
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
        if _is_internal_gateway_turn(user_content) or is_trivial_prompt(user_content):
            return
        sid = session_id or self._session_id

        def _sync():
            try:
                blob = f"USER: {user_content[:1500]}\nASSISTANT: {(assistant_content or '')[:1500]}"
                self._insert(
                    memory_id=f"turn_{uuid.uuid4().hex[:16]}",
                    session_id=sid,
                    role="turn",
                    content=blob,
                    target="memory",
                    metadata={"kind": "turn"},
                )
            except Exception as e:
                logger.warning("oracle sync_turn failed: %s", e)

        t = threading.Thread(target=_sync, daemon=True, name="oracle-sync")
        with self._lock:
            prev = self._sync_thread
            self._sync_thread = t
        if prev and prev.is_alive():
            prev.join(timeout=2.0)
        t.start()

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self._pool or action != "add" or not content:
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
        if not self._pool or not messages:
            return
        try:
            user_bits = [
                (m.get("content") or "")[:400]
                for m in messages
                if m.get("role") == "user" and isinstance(m.get("content"), str)
            ]
            sample = " | ".join(
                b for b in user_bits[-4:] if b and not _is_internal_gateway_turn(b)
            )
            if len(sample) < 20:
                return
            self._insert(
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
        self._session_id = new_session_id or self._session_id

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
                vec = self._embed(content)
                txt = content[:4000]
                with self._pool.acquire() as conn:
                    with conn.cursor() as cur:
                        if self._onnx_model:
                            cur.execute(
                                f"""
                                UPDATE {self._table}
                                SET content = :1, embedding = VECTOR_EMBEDDING({self._onnx_model} USING :2 AS DATA), content_txt = :3
                                WHERE memory_id = :4
                                """,
                                (content, txt, txt, mid),
                            )
                        else:
                            cur.execute(
                                f"""
                                UPDATE {self._table}
                                SET content = :1, embedding = :2, content_txt = :3
                                WHERE memory_id = :4
                                """,
                                (content, vec, txt, mid),
                            )
                        n = cur.rowcount
                    conn.commit()
                if not n:
                    return tool_error(f"Memory not found: {mid}")
                if vec is None and not self._onnx_model:
                    self._queue_embed_backfill(mid, content)
                return json.dumps({"result": "updated", "memory_id": mid})
            if tool_name == "oracle_delete":
                mid = args.get("memory_id")
                if not mid:
                    return tool_error("memory_id required")
                with self._pool.acquire() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"DELETE FROM {self._table} WHERE memory_id = :1",
                            (mid,),
                        )
                        n = cur.rowcount
                    conn.commit()
                if not n:
                    return tool_error(f"Memory not found: {mid}")
                return json.dumps({"result": "deleted", "memory_id": mid})
            if tool_name == "oracle_chunk":
                content = (args.get("content") or "").strip()
                if not content:
                    return tool_error("Missing required parameter: content")
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
                    import oracledb

                    for row in cur:
                        val = (
                            row[0].read()
                            if isinstance(row[0], oracledb.LOB)
                            else row[0]
                        )
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
        for i, ch in enumerate(chunks):
            mid = f"chk_{uuid.uuid4().hex[:16]}"
            self._insert(
                memory_id=mid,
                session_id=self._session_id,
                role="chunk",
                content=ch,
                target="memory",
                metadata={"kind": "chunk", "index": i},
            )
            ids.append(mid)
        return ids

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_thread, self._embed_worker):
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
