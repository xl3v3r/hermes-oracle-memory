"""Oracle 26ai memory plugin — MemoryProvider for Autonomous AI Database.

Vector search (in-memory HNSW index) + Oracle Text + JSON as the primary Hermes memory backend.
Credentials: OCI_DB_USER / OCI_DB_PASSWORD / OCI_DB_DSN in $HERMES_HOME/.env.
Embeddings: In-database ONNX model (768-d BGE_BASE_EN_V1_5 via VECTOR_EMBEDDING) strictly
in-database. Keeping all data 100% in-house with zero external third-party endpoint fallback.
"""

from __future__ import annotations

import array
import decimal
import html
import importlib.util
import itertools
import json
import logging
import math
import os
import queue
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt
from agent.secret_scope import get_secret
from tools.registry import tool_error

try:  # Hermes copies the caller's context (profile home, secret scope) into the thread
    from agent.memory_provider import spawn_context_thread as _spawn_context_thread
except ImportError:  # older Hermes
    _spawn_context_thread = None

logger = logging.getLogger(__name__)

__version__ = "1.1.1.5"

VECTOR_INDEX_DDL = (
    "CREATE VECTOR INDEX {index_name} ON {table_name} (embedding) ORGANIZATION INMEMORY NEIGHBOR GRAPH"
)

# Bootstrap DDL. INSTALL.md lists the same statements for installs that manage
# the schema themselves (HERMES_ORACLE_SCHEMA_POLICY=REQUIRE_EXISTING).
TABLE_DDL = """CREATE TABLE {table_name} (
    memory_id VARCHAR2(128) PRIMARY KEY,
    session_id VARCHAR2(128),
    agent_id VARCHAR2(64),
    role VARCHAR2(64),
    content CLOB,
    embedding VECTOR(768, FLOAT32),
    created_at TIMESTAMP DEFAULT SYSTIMESTAMP,
    target VARCHAR2(32) DEFAULT 'memory',
    metadata JSON,
    content_txt VARCHAR2(4000),
    embedding_model VARCHAR2(64),
    embedding_updated_at TIMESTAMP(6),
    turn_index NUMBER,
    chunk_index NUMBER,
    turn_id VARCHAR2(64),
    conv_ref VARCHAR2(512)
)"""
# A plain Oracle Text search index. CONTAINS(content, ...) needs it, and so
# does every INSERT once it exists: a domain index left in the FAILED state
# rejects DML with ORA-29861.
TEXT_INDEX_DDL = "CREATE SEARCH INDEX {index_name} ON {table_name} (content)"
# Columns added after the first release; ALTER TABLE brings older tables up to date.
_UPGRADE_COLUMNS = (
    ("created_at", "TIMESTAMP DEFAULT SYSTIMESTAMP"),
    ("target", "VARCHAR2(32) DEFAULT 'memory'"),
    ("metadata", "JSON"),
    ("content_txt", "VARCHAR2(4000)"),
    ("embedding_model", "VARCHAR2(64)"),
    ("embedding_updated_at", "TIMESTAMP(6)"),
    ("turn_index", "NUMBER"),
    ("chunk_index", "NUMBER"),
    ("turn_id", "VARCHAR2(64)"),
    ("conv_ref", "VARCHAR2(512)"),
)
# "The object is already there" for bootstrap DDL.
_DDL_ALREADY_EXISTS = (
    "ORA-00955",  # name is already used by an existing object
    "ORA-01430",  # column being added already exists in table
    "ORA-01408",  # such column list already indexed
    "ORA-29879",  # cannot create multiple domain indexes on a column list using same indextype
    "ORA-29880",  # such column list already indexed using another domain index and indextype
)
_INDEX_STATE_SQL = (
    "SELECT i.index_name, i.index_type, i.index_subtype, i.ityp_name, i.domidx_opstatus, c.column_name "
    "FROM user_indexes i LEFT JOIN user_ind_columns c ON c.index_name = i.index_name "
    "WHERE i.table_name = UPPER(:t)"
)
# (pool fingerprint, warning) pairs already logged at warning level.
_SCHEMA_WARNINGS_LOGGED: set[tuple[str, str]] = set()

_51815_RETRIES = 3
_51815_BACKOFF_SECS = 0.5


def _is_51815(err: Any) -> bool:
    return "ORA-51815" in str(err)

def _setting(name: str, default: str = "") -> str:
    """A setting of the active profile: its secret scope (the profile's .env)
    first, then the process environment. Hermes binds the scope to the turn's
    context, so inside initialize(), a tool call or a context thread this is
    the profile's own value. Without a scope (import time, a plain thread)
    only the process environment is read."""
    try:
        value = get_secret(name)
    except Exception:  # no scope on a multiplexed gateway: never guess a profile
        value = None
    if isinstance(value, str) and value.strip():
        return value.strip()
    raw = os.environ.get(name)
    return raw.strip() if isinstance(raw, str) and raw.strip() else default


def _spawn_thread(target: Any, *, name: str, args: tuple = (), kwargs: dict | None = None) -> threading.Thread:
    """An unstarted daemon thread for a background job. With Hermes'
    spawn_context_thread it runs under the caller's context, so the job sees
    the profile home and secret scope of the session that started it."""
    if _spawn_context_thread is not None:
        return _spawn_context_thread(target, name=name, daemon=True, args=args, kwargs=kwargs)
    return threading.Thread(target=target, name=name, daemon=True, args=args, kwargs=kwargs)


def _hermes_home_path(value: Any = None) -> Path | None:
    """The Hermes home: *value* (the hermes_home Hermes passes to
    initialize()), else the active profile's home, else $HERMES_HOME, else
    ~/.hermes. None when no home can be determined."""
    try:
        if isinstance(value, (str, os.PathLike)) and str(value).strip():
            return Path(str(value)).expanduser()
        try:
            from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

            return Path(get_hermes_home())
        except Exception:
            pass
        env = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(env).expanduser() if env else Path.home() / ".hermes"
    except (OSError, RuntimeError, KeyError):  # HOME unset and no pwd entry
        return None


def _env_number_opt(name: str, *, allow_zero: bool = False) -> float | None:
    """Positive finite number from the settings (int or float text), or None
    when unset, empty, or invalid (invalid values are logged). *allow_zero*
    also accepts 0."""
    raw = _setting(name)
    if not raw:
        return None
    try:
        val = float(raw)
        if not math.isfinite(val) or val < 0 or (val == 0 and not allow_zero):
            raise ValueError
    except ValueError:
        logging.getLogger(__name__).warning("ignoring invalid %s=%r; using the default", name, raw)
        return None
    return val


def _env_number(
    name: str, default: float, *, lo: float | None = None, hi: float | None = None, allow_zero: bool = False
) -> float:
    """Like _env_number_opt, falling back to *default*; an explicit value is
    clamped to [lo, hi] when given."""
    val = _env_number_opt(name, allow_zero=allow_zero)
    if val is None:
        return default
    if lo is not None:
        val = max(lo, val)
    if hi is not None:
        val = min(hi, val)
    return val


# How long prefetch() waits for this turn's recall before returning empty (the
# search keeps running and is reused if the same query comes back). Must stay
# under the host's external-prefetch budget (agent/memory_manager.py: 8 s).
_PREFETCH_WAIT_SECS = _env_number("HERMES_ORACLE_PREFETCH_WAIT", 3.0, lo=0.1, hi=7.0)
_EMBED_DIM = 768
# How much of a row's text goes to the embedding model.
_EMBED_TEXT_CHARS = 1200
_EMBED_CACHE_MAX = 256
_EMBED_BATCH = 32
_ONNX_RETRY_AFTER_SECS = 60.0
# ORA errors where the embedding engine/service misbehaved but the row is
# fine: degrade to NULL-embedding insert + background backfill (turn/skill
# rows must not be lost to a transient server condition).
_ONNX_TRANSIENT_PREFIXES = (
    "ORA-01013",  # user specified cancel / statement canceled
    "ORA-04031",  # insufficient PGA memory
    "ORA-10859",  # connection load too high
    "ORA-12801",  # parallel server: not enough memory
    "ORA-12826",  # slave: out of memory area
    "DPY-4024",  # client call_timeout: DB slow, not an ONNX model failure
    # Session/connection-state errors: the connection is bad, not the model or
    # the row. Degrade to NULL-embedding insert + backfill on a fresh connection.
    "ORA-00028",  # session killed
    "ORA-01001",  # invalid cursor
    "ORA-01002",  # fetch out of sequence
    "ORA-03113",  # end-of-file on communication channel
    "ORA-03114",  # not connected
    "ORA-03135",  # connection lost contact
)

# Errors that say the *connection/session* is broken (or the client timed out)
# rather than the in-database ONNX model. These must never trip the ONNX
# cooldown: doing so locked out every embedding read/write for 60s after an
# ORA-01002 on a single pooled connection.
_SESSION_ERROR_MARKERS = (
    "ORA-00028", "ORA-00603", "ORA-01001", "ORA-01002", "ORA-01012", "ORA-01013",
    "ORA-02396", "ORA-03113", "ORA-03114", "ORA-03135", "ORA-12170", "ORA-12537",
    "ORA-12541", "ORA-25408",
    "DPI-1067", "DPI-1040", "DPY-4005", "DPY-4011", "DPY-4024", "DPY-1001", "DPY-1002",
)
_ORA_CODE_RE = re.compile(r"\bORA-\d{5}\b")

# Client/pool timeouts: the caller's budget is already spent, so never retry.
# (DPI-1040 / DPY-4005 = pool wait exceeded; DPI-1067 / DPY-4024 = call timeout;
# ORA-01013 = the server cancelled the call, which is how a call timeout ends.)
_TIMEOUT_MARKERS = ("DPI-1040", "DPI-1067", "DPY-4005", "DPY-4024", "ORA-01013")
_TIMEOUT_PHRASE_RE = re.compile(r"\b(?:timed out|time ?out)\b", re.IGNORECASE)


def _is_timeout_error(err: Any) -> bool:
    """A client/pool/call timeout. The bare words "timeout"/"timed out" count only
    in messages without an ORA- code (Python and driver errors): an unrelated
    server error that merely mentions a timeout (e.g. ORA-12170 "TNS:Connect
    timeout", ORA-04021 "timeout occurred while waiting to lock object") is not
    a search timeout and must keep its own handling."""
    if isinstance(err, TimeoutError):
        return True
    s = str(err)
    if any(m in s for m in _TIMEOUT_MARKERS):
        return True
    return not _ORA_CODE_RE.search(s) and bool(_TIMEOUT_PHRASE_RE.search(s))

# Row/data/constraint errors: the statement was wrong for this row (duplicate
# caller-chosen memory_id, value too large, bad JSON), not the ONNX model.
_DATA_ERROR_MARKERS = (
    "ORA-00001",  # unique constraint (duplicate memory_id)
    "ORA-00942",  # table or view does not exist
    "ORA-01400",  # cannot insert NULL
    "ORA-01438",  # value larger than precision
    "ORA-02290", "ORA-02291", "ORA-02292",  # check / FK constraints
    "ORA-12899",  # value too large for column
    "ORA-40441",  # JSON syntax error
    "ORA-00060",  # deadlock detected: lock contention (e.g. bulk sync vs. lifecycle upsert)
)


class _TxAborted(RuntimeError):
    """The caller's transaction (``tx_conn``) can no longer be used: the session
    broke, or ROLLBACK TO SAVEPOINT itself failed. Never retried on that
    connection, never answered with a NULL-embedding fallback there, and never
    a reason for the ONNX cooldown. The caller rolls the whole transaction back."""


class _PolicyError(RuntimeError):
    """Raised by this plugin's own checks before any SQL runs (for example "the
    embedding model is not configured"). Says nothing about the connection, so
    _acquire_checked() must not drop a healthy pooled session for it."""


def _is_session_error(err: Any) -> bool:
    """True when *err* indicates a bad connection/session or client timeout."""
    s = str(err)
    return _is_timeout_error(err) or any(m in s for m in _SESSION_ERROR_MARKERS)


def _is_onnx_model_failure(err: Any) -> bool:
    """Only a server-side ORA- error that is not a session/connection error can
    indicate a broken ONNX model. Client/driver (DPY-/DPI-) errors and plain
    Python exceptions (e.g. "'NoneType' object is not iterable") cannot."""
    if err is None:
        return True  # legacy callers without context keep the old behaviour
    if _is_session_error(err):
        return False
    if any(m in str(err) for m in _DATA_ERROR_MARKERS):
        return False
    return bool(_ORA_CODE_RE.search(str(err)))


# Live (initialized, pool open) providers in this process. The plugin's global
# on_skill_lifecycle hook is re-bound to the NEWEST provider instance on every
# load; when that instance's agent shuts down, the hook would otherwise keep
# writing through a provider whose pool is gone ("pool not initialized").
import weakref as _weakref

_LIVE_PROVIDERS: "_weakref.WeakSet[Any]" = _weakref.WeakSet()
_LIVE_PROVIDERS_LOCK = threading.Lock()
_LIVE_SEQ = itertools.count(1)


def _register_live_provider(provider: Any) -> None:
    """Add *provider* to the live registry, stamping registration order so the
    hook fallback can pick the newest one (WeakSet iteration is unordered)."""
    with _LIVE_PROVIDERS_LOCK:
        provider._live_seq = next(_LIVE_SEQ)
        _LIVE_PROVIDERS.add(provider)


# oracle_chunk(file_path=...) stores the file in the SHARED database, where any
# later oracle_search on any host can read it back. A prompt-injected call must
# not be able to copy credentials there, so refuse well-known secret locations.
def _is_pool_busy_error(err: BaseException) -> bool:
    """python-oracledb refuses a non-forced close while connections are out."""
    return "DPY-1005" in str(err)


def _close_pool_when_idle(pool: Any, budget: float | None = None, poll: float = 0.5) -> None:
    """Close *pool* once every borrowed connection is back. Force only after the
    longest call budget (+ pool wait) has expired, when any holder has already
    timed out or finished."""
    if budget is None:
        budget = max(_SEARCH_TIMEOUT_SECS, _ingest_timeout_secs()) + _pool_wait_timeout_ms() / 1000.0 + 30.0
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        time.sleep(poll)
        try:
            pool.close()
            return
        except Exception as e:
            if not _is_pool_busy_error(e):
                logger.debug("oracle deferred pool close: %s", e)
                return
    logger.info("oracle deferred pool close: connections still borrowed after %.0fs; forcing close", budget)
    try:
        pool.close(force=True)
    except Exception as e:
        logger.warning("oracle deferred pool close (forced) failed: %s", e)


def _retire_pool(pool: Any, reason: str) -> None:
    """Close a pool this provider no longer uses. A non-forced close fails while
    connections are borrowed (DPY-1005); then close it in the background once
    they come back, exactly like the normal teardown path."""
    if pool is None:
        return
    try:
        pool.close()
        logger.info("oracle: closed %s pool", reason)
    except Exception as e:
        if not _is_pool_busy_error(e):
            logger.debug("oracle %s pool close: %s", reason, e)
            return
        logger.info("oracle %s pool close deferred until busy connections return: %s", reason, e)
        _spawn_thread(_close_pool_when_idle, args=(pool,), name="oracle-pool-close").start()


_RESERVED_SESSION_IDS = frozenset({"skills", "none", "null"})
# oracle_delete(session_id=...) removes a whole session. A call deletes at most
# this many rows unless it names a higher max_rows, and never more than the
# hard maximum: larger purges are an operator job in SQL.
_PURGE_DEFAULT_MAX_ROWS = 200
_PURGE_HARD_MAX_ROWS = 10000

_SENSITIVE_DIR_NAMES = frozenset({
    ".ssh", ".gnupg", ".aws", ".oci", ".kube", ".docker", ".azure", ".gcloud",
    ".password-store", ".pki", ".secrets",
})
_SENSITIVE_FILE_NAMES = frozenset({
    ".env", "auth.json", ".netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc",
    "credentials", "credentials.json", "cwallet.sso", "ewallet.p12", "ewallet.pem",
    "keystore.jks", "truststore.jks", "shadow", "gshadow",
    "application_default_credentials.json", "credentials.db", "access_tokens.db",
    "credentials.toml", "credentials.tfrc.json", ".envrc",
    ".bash_history", ".zsh_history", ".python_history", ".psql_history", ".mysql_history",
})
# Tool config dirs under ~/.config that hold tokens (gcloud, GitHub CLI, ...).
_SENSITIVE_CONFIG_DIRS = frozenset({"gcloud", "gh", "hub", "doctl", "rclone", "op", "1password"})
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".kdbx", ".env", ".sso")
_SENSITIVE_ROOTS = (
    "/proc", "/sys", "/dev", "/etc", "/boot", "/root", "/var/lib", "/run",
    # macOS: resolve() maps /etc and /var to /private/...
    "/private/etc", "/private/var/root", "/private/var/db", "/private/var/lib", "/private/var/run",
)
# Sensitive roots that are also a legitimate home directory (Hermes running as root).
_HOME_LIKE_ROOTS = frozenset({"/root", "/private/var/root"})

# macOS (APFS): the data volume is mounted at /System/Volumes/Data and
# firmlinked into "/", so /System/Volumes/Data/Users/u and /Users/u are one
# directory. Path.resolve() keeps both spellings, so path checks compare the
# short one.
_IS_DARWIN = sys.platform == "darwin"
_DARWIN_DATA_VOLUME = "/System/Volumes/Data"


def _volume_canonical(p: Path) -> Path:
    """*p* without a leading /System/Volumes/Data on macOS; unchanged elsewhere.
    For path checks only: the two spellings are the same directory for the
    firmlinked trees (/Users, /private, ...), not for every path."""
    if not _IS_DARWIN:
        return p
    sp = str(p)
    if sp == _DARWIN_DATA_VOLUME:
        return Path("/")
    if sp.startswith(_DARWIN_DATA_VOLUME + "/"):
        return Path(sp[len(_DARWIN_DATA_VOLUME):])
    return p


def _trusted_home_roots() -> list[Path]:
    """The user's home and HERMES_HOME, exempt from the system-root check (but
    not from the credential name checks): when Hermes runs as root its home is
    /root, and service installs may use HERMES_HOME=/var/lib/hermes."""
    hermes_home = _hermes_home_path()  # the active profile's home inside Hermes
    raws = [str(hermes_home) if hermes_home is not None else ""]
    try:
        raws.append(str(Path.home()))
    except (OSError, RuntimeError, KeyError):  # HOME unset and no pwd entry
        pass
    out = []
    for raw in raws:
        if not raw:
            continue
        try:
            r = _volume_canonical(Path(raw).expanduser().resolve())
        except (OSError, RuntimeError, KeyError):
            continue
        if len(r.parts) < 2:  # never exempt "/"
            continue
        # A misconfigured HOME/HERMES_HOME (/etc, /var, /var/lib) must not exempt
        # a whole system root; /root itself is a real home and stays allowed.
        if any(Path(sr).is_relative_to(r) and not (str(r) == sr and sr in _HOME_LIKE_ROOTS)
               for sr in _SENSITIVE_ROOTS):
            continue
        out.append(r)
    return out


def _sensitive_path_reason(p: Path) -> str:
    """Non-empty reason when *p* (already resolved) looks like a secret store."""
    p = _volume_canonical(p)
    sp = str(p)
    if sp == "/":
        return "system path"
    if any(sp == r or sp.startswith(r + "/") for r in _SENSITIVE_ROOTS) and not any(
        p.is_relative_to(h) for h in _trusted_home_roots()
    ):
        return "system path"
    parts = [x.lower() for x in p.parts]
    if any(x in _SENSITIVE_DIR_NAMES for x in parts) or any(
        a == ".config" and b in _SENSITIVE_CONFIG_DIRS for a, b in zip(parts, parts[1:])
    ):
        return "credentials directory"
    name = parts[-1] if parts else ""
    if (name in _SENSITIVE_FILE_NAMES or name.startswith(".env.") or name.endswith(_SENSITIVE_SUFFIXES)
            or re.fullmatch(r"id_(rsa|dsa|ecdsa|ed25519)(_sk)?(\.pub)?", name)):
        return "credentials file"
    return ""


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def _is_broad_scan_root(dp: Path) -> bool:
    """True when *dp* (already resolved) is too broad to scan for skills:

    - "/" or a top-level directory;
    - the home directory or one of its ancestors, by path or by file identity
      (an alias such as an APFS firmlink or a bind mount is a second path
      that resolve() does not normalise);
    - a system or credential path, or an ancestor of a system root;
    - on macOS, the data volume or an ancestor of it.
    """
    if _IS_DARWIN and Path(_DARWIN_DATA_VOLUME).is_relative_to(dp):
        return True
    canon = _volume_canonical(dp)
    if len(canon.parts) < 3:
        return True
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError, KeyError):
        home = None
    if home is not None:
        if _volume_canonical(home).is_relative_to(canon):
            return True
        if any(_same_file(dp, a) for a in (home, *home.parents)):
            return True
    if _sensitive_path_reason(canon):
        return True
    return any(Path(sr).is_relative_to(canon) for sr in _SENSITIVE_ROOTS)


def _as_bool(v: Any) -> bool:
    """Tool-argument boolean: bool("false") is True, so parse text explicitly."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


_SYNC_MOD_NAME = "hermes_oracle_sync_skills"
_SYNC_MOD_LOCK = threading.Lock()


def _sync_skills_module() -> Any:
    """scripts/sync_skills.py, loaded by file path and cached. Never use
    `import scripts.sync_skills` in-process: inside Hermes the top-level name
    `scripts` is the host's own package, so that import fails (silently, in the
    skill-lifecycle hook)."""
    with _SYNC_MOD_LOCK:
        mod = sys.modules.get(_SYNC_MOD_NAME)
        if mod is None:
            path = Path(__file__).resolve().parent / "scripts" / "sync_skills.py"
            if not path.exists():
                raise ImportError(f"Cannot find sync_skills script at {path}")
            mod = _load_module_from_path(_SYNC_MOD_NAME, path)
        return mod


# Skill names as stored in ids (skill:<host>:<name>:c<N>) and accepted from hooks/tools.
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# Lifecycle writer bookkeeping is per process, keyed by (pool fingerprint,
# table, skill): events reach whichever provider serves the session, and two
# instances must never run two writers for one skill at the same time (#6).
_LIFECYCLE_LOCK = threading.Lock()
_LIFECYCLE_IN_FLIGHT: set[tuple[str, str, str]] = set()
_LIFECYCLE_PENDING: dict[tuple[str, str, str], tuple[Any, str, list[Path]]] = {}


def _skill_md_matches(root: Path, skill_name: str) -> list[Path]:
    """<root>/*/<skill_name>/SKILL.md, ignoring hidden trees such as .archive/
    (Hermes moves archived skills there before emitting 'archived')."""
    out = []
    for sub in root.glob(f"*/{skill_name}/SKILL.md"):
        try:
            rel = sub.relative_to(root).parts
        except ValueError:
            continue
        if not any(part.startswith(".") for part in rel):
            out.append(sub)
    return out


# ---------------------------------------------------------------------------
# Handoff rows: caller-chosen stable memory_id and/or tags (1.1.1.3)
# ---------------------------------------------------------------------------
# A stable id lets one agent write "handoff:<project>:<topic>" and the next one
# read/overwrite exactly that row; tags (metadata.tags, lower-cased) let a reader
# pull every row of a handoff set without vector search or the ONNX model.
_MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/\-]{0,127}$")
_MAX_TAGS = 32
_MAX_TAG_LEN = 64
# Tool-surface bounds (1.1.1.4). One oracle_add call stores at most
# _ADD_MAX_FACTS facts of at most _MAX_FACT_CHARS characters each (longer text
# belongs in oracle_chunk); oracle_search returns at most
# _SEARCH_CONTENT_MAX_CHARS characters of each row (oracle_get has the rest).
_ADD_MAX_FACTS = 100
_MAX_FACT_CHARS = 32000
_SEARCH_CONTENT_MAX_CHARS = 4000
# DBMS_VECTOR.UTL_TO_CHUNKS by words accepts max 10..1000 (ORA-30584 outside).
_CHUNK_MIN_WORDS = 10
_CHUNK_MAX_WORDS = 1000
# Skill-corpus ids are owned by the skill sync; the generic tools refuse them.
_SKILL_ID_PREFIX = "skill:"
_GET_DEFAULT_LIMIT = 20
_GET_MAX_LIMIT = 100


def _id_list(raw: Any) -> list[Any]:
    """memory_ids as a list. LLM callers often pass one id or a comma/space
    separated (or JSON-array) string; list("abc") would split it into chars."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        txt = raw.strip()
        if txt.startswith("["):
            try:
                parsed = json.loads(txt)
            except ValueError:
                raise ValueError("memory_ids: invalid JSON array") from None
            return list(parsed) if isinstance(parsed, list) else [parsed]
        return [p for p in re.split(r"[,\s]+", txt) if p]
    if isinstance(raw, (list, tuple, set)):
        return list(raw)
    return [raw]


def _normalize_memory_id(value: Any) -> str:
    """Validate a caller-supplied memory_id (1-128 chars, [A-Za-z0-9_.:@/-], no
    leading punctuation). Raises ValueError with a model-readable message."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("memory_id must be a non-empty string")
    mid = value.strip()
    if not _MEMORY_ID_RE.match(mid):
        raise ValueError(
            "memory_id must be 1-128 chars of letters, digits, and _ . : @ / - "
            "and start with a letter or digit"
        )
    return mid


def _normalize_tags(value: Any) -> list[str]:
    """Accept a list, a JSON-array string, or a comma-separated string. Tags are
    stripped, lower-cased, de-duplicated (order kept). Raises ValueError when a
    tag is empty/too long, contains control characters, or there are too many."""
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("[") and raw.endswith("]"):
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                value = parsed
            else:
                value = raw.strip("[]").split(",")
        else:
            value = raw.split(",") if raw else []
    if not isinstance(value, (list, tuple, set)):
        raise ValueError("tags must be a list of strings")
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        tag = " ".join(str(item).split()).lower()
        if not tag:
            continue
        if len(tag) > _MAX_TAG_LEN:
            raise ValueError(f"tag too long (max {_MAX_TAG_LEN} chars): {tag[:20]}...")
        if any(ord(ch) < 32 for ch in tag):
            raise ValueError("tags must not contain control characters")
        if tag not in out:
            out.append(tag)
    if len(out) > _MAX_TAGS:
        raise ValueError(f"too many tags (max {_MAX_TAGS})")
    return out


def _tag_filter_sql(tags: Any, match: str = "all", prefix: str = "tg") -> tuple[str, dict]:
    """SQL predicate (leading ' AND ') matching metadata.tags. Tag values are bound
    through JSON_EXISTS ... PASSING, never interpolated.

    The path is applied to JSON_QUERY(metadata, '$.tags') rather than to the
    column itself on purpose: a JSON search index on metadata (<TABLE>_JSON_IDX
    on existing installs) would otherwise answer the predicate, and that index
    syncs asynchronously -- a handoff row written and read back within ~2 s was
    not found. Wrapping the column makes Oracle evaluate the tags on the rows
    themselves (read-your-writes). Matching is case-insensitive: the query tags
    are lower-cased by _normalize_tags and the stored ones by @.lower(), so rows
    written by other tools with mixed-case tags still match."""
    norm = _normalize_tags(tags)
    if not norm:
        return "", {}
    binds: dict[str, str] = {}
    preds: list[str] = []
    for i, tag in enumerate(norm):
        name = f"{prefix}{i}"
        binds[name] = tag
        preds.append(
            f"JSON_EXISTS(JSON_QUERY(metadata, '$.tags' RETURNING JSON), '$[*]?(@.lower() == ${name})' "
            f"PASSING :{name} AS \"{name}\")"
        )
    joiner = " OR " if str(match or "all").lower() == "any" else " AND "
    return " AND (" + joiner.join(preds) + ") ", binds


def _lob_text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "read"):
        value = value.read()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _execute_lobs_as_str(cur: Any, sql: str, binds: Any) -> None:
    """Execute a query with CLOB columns fetched as str in the same round trip
    (fetch_lobs=False). Fetching locators costs one more round trip per row
    for every LOB value read. The value is not truncated (checked with a
    96 KB CLOB on python-oracledb 26.1 thin). Every caller of one SQL text must
    use the same setting: a connection's statement cache keeps the first
    execution's column types for identical SQL."""
    try:
        cur.execute(sql, binds, fetch_lobs=False)
    except TypeError as e:
        if "fetch_lobs" not in str(e):
            raise
        cur.execute(sql, binds)  # driver without the per-execute option


def _metadata_dict(value: Any) -> dict:
    if value is None:
        return {}
    if hasattr(value, "read"):
        value = value.read()
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return {}
    return dict(value) if isinstance(value, dict) else {}


_SKILL_ROW_READONLY = (
    "skill-corpus rows (role='skill') are written by oracle_sync_skills and the skill "
    "lifecycle hook; edit the SKILL.md and re-sync instead"
)


def _search_tool_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """oracle_search result: each row's content is cut to
    _SEARCH_CONTENT_MAX_CHARS (flagged, with its full length) and internal
    ranking fields are dropped."""
    out_rows = []
    cut = False
    for r in rows:
        if not isinstance(r, dict):
            continue
        item = {k: v for k, v in r.items() if k not in ("dist",)}
        body = item.get("content")
        if isinstance(body, str) and len(body) > _SEARCH_CONTENT_MAX_CHARS:
            item["content"] = body[:_SEARCH_CONTENT_MAX_CHARS]
            item["content_truncated"] = True
            item["content_chars"] = len(body)
            cut = True
        out_rows.append(item)
    payload: dict[str, Any] = {"results": out_rows, "count": len(out_rows)}
    if cut:
        payload["note"] = "Some contents were cut; oracle_get(memory_id=...) returns the full text."
    return payload


def _chunk_params(max_words: Any, overlap: Any) -> tuple[int, int]:
    """Validate oracle_chunk sizes: max_words 10..1000 (the UTL_TO_CHUNKS range)
    and 0 <= overlap <= max_words / 2. Raises ValueError."""
    try:
        mw = 80 if max_words in (None, "") else int(max_words)
        ov = 10 if overlap in (None, "") else int(overlap)
    except (TypeError, ValueError):
        raise ValueError("max_words and overlap must be integers") from None
    if not _CHUNK_MIN_WORDS <= mw <= _CHUNK_MAX_WORDS:
        raise ValueError(f"max_words must be between {_CHUNK_MIN_WORDS} and {_CHUNK_MAX_WORDS} (got {mw})")
    if not 0 <= ov <= mw // 2:
        raise ValueError(f"overlap must be between 0 and max_words/2 = {mw // 2} (got {ov})")
    return mw, ov


def _metadata_tags(value: Any) -> list[str]:
    """Tags projected as JSON_QUERY(metadata, '$.tags') text (or a list)."""
    if value is None:
        return []
    if hasattr(value, "read"):
        value = value.read()
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [str(t) for t in value if isinstance(t, (str, int, float))] if isinstance(value, list) else []


class _CallTimeoutPool:
    """Pool proxy that re-applies the default call_timeout on EVERY acquire.

    python-oracledb runs session_callback only for newly created connections,
    so a pooled connection whose call_timeout was tightened by a read path
    (search/health/list -> search clamp) kept it when later reused by a write
    -> DPY-4024 on inserts, which then tripped the ONNX cooldown
    ("embedding model is unavailable"). Read paths still override after acquire.
    """

    def __init__(self, pool: Any, default_ms: int) -> None:
        self._p = pool
        self._default_ms = default_ms

    def acquire(self, *args: Any, **kwargs: Any) -> Any:
        conn = self._p.acquire(*args, **kwargs)
        try:
            conn.call_timeout = self._default_ms
        except Exception:
            pass
        return conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._p, name)
_FETCH_ARRAYSIZE = 64
# Hybrid search: a row found only by Oracle Text scores 0.3 x its text score
# (vector hits score 0.7 x similarity), on the fused path and on the
# cooldown / degraded text-only paths alike.
_HYBRID_TEXT_WEIGHT = 0.3
# Embedding backfill (rows written with a NULL embedding during an ONNX cooldown
# or a transient error). Failed batches back off exponentially; a row is dropped
# from the in-memory queue after _BACKFILL_MAX_ATTEMPTS (it keeps its NULL
# embedding in the table and is picked up again by the startup re-queue scan).
_BACKFILL_BASE_DELAY_SECS = 2.0
_BACKFILL_MAX_DELAY_SECS = 300.0
_BACKFILL_MAX_ATTEMPTS = 8
_BACKFILL_STARTUP_LIMIT = 500
_BACKFILL_SCANNED: set[str] = set()  # pool fingerprints already scanned in this process
_BACKFILL_SCANNED_LOCK = threading.Lock()
# --- Timeouts (1.1.1.3) -------------------------------------------------------
# Read-path budget for one recall query (oracle_search / prefetch / oracle_get).
# HERMES_ORACLE_SEARCH_TIMEOUT_MS is the documented knob; before 1.1.1.3 the
# per-query clamp read HERMES_ORACLE_SEARCH_TIMEOUT (seconds) instead, so the
# documented variable never took effect. The seconds form is still honored as
# a legacy fallback. 13 s was too short: observed recall p99 was ~14 s.
_DEFAULT_SEARCH_TIMEOUT_MS = 30000
_DEFAULT_INGEST_TIMEOUT_SECS = 600.0
# Max wait for a pooled connection. Before 1.1.1.3 the pool used
# POOL_GETMODE_WAIT, which ignores wait_timeout and blocks forever when every
# session is busy (observed: oracle_search hung 403 s until the user stopped it).
_DEFAULT_POOL_WAIT_TIMEOUT_MS = 20000
# Idle seconds before sessions above `min` are closed (was 5 s, which forced a
# fresh TLS handshake to ADB for most bursts).
_DEFAULT_POOL_IDLE_TIMEOUT_SECS = 300


def _search_timeout_ms() -> int:
    ms = _env_number_opt("HERMES_ORACLE_SEARCH_TIMEOUT_MS")
    if ms is None:
        secs = _env_number_opt("HERMES_ORACLE_SEARCH_TIMEOUT")  # legacy (seconds)
        ms = secs * 1000 if secs is not None else _DEFAULT_SEARCH_TIMEOUT_MS
    return int(max(1000, min(ms, 600000)))


def _ingest_timeout_secs() -> float:
    return _env_number("HERMES_ORACLE_INGEST_TIMEOUT", _DEFAULT_INGEST_TIMEOUT_SECS, lo=30.0, hi=7200.0)


def _pool_wait_timeout_ms() -> int:
    return int(_env_number("HERMES_ORACLE_POOL_WAIT_TIMEOUT_MS", _DEFAULT_POOL_WAIT_TIMEOUT_MS, lo=1000, hi=300000))


def _pool_idle_timeout_secs() -> int:
    return int(_env_number("HERMES_ORACLE_POOL_IDLE_TIMEOUT", _DEFAULT_POOL_IDLE_TIMEOUT_SECS, lo=30, hi=86400))


_SEARCH_TIMEOUT_MS = _search_timeout_ms()
_SEARCH_TIMEOUT_SECS = _SEARCH_TIMEOUT_MS / 1000.0
_INGEST_TIMEOUT_SECS = _ingest_timeout_secs()
_INGEST_TIMEOUT_MS = int(_INGEST_TIMEOUT_SECS * 1000)
_SKILL_FETCH_MAX_CHUNKS = int(_env_number("HERMES_ORACLE_SKILL_FETCH_MAX_CHUNKS", 400, lo=1))
_EMBED_MAX_RETRIES = 2
_CONSERVATIVE_EMBED_CHARS = 400
_RERANK_MAX_DOCS = 8
_RERANK_ENABLED = _setting("HERMES_ORACLE_DISABLE_RERANK").lower() not in ("1", "true", "yes")
_WEIGHT_RECENCY_FLOOR = _env_number("HERMES_ORACLE_RECENCY_FLOOR", 0.85, hi=1.0, allow_zero=True)
_WEIGHT_RECENCY_SCALE_DAYS = _env_number("HERMES_ORACLE_RECENCY_SCALE_DAYS", 120.0)
# Role preference is applied HERE, after candidates are selected, and must never be
# pushed back into the SQL top-k cut. Encoding "facts first" as an ORDER BY term ahead
# of the similarity key inside `FETCH FIRST :k` starves every non-fact role out of the
# window whenever the fact table is larger than k: with ~1.4k facts and ~31.7k skill
# chunks, every skill query returned zero skill rows (defect fixed in v1.0.9.6).
# Skills are reference material, so they rank just below first-person facts at equal
# relevance while still winning outright when they are the closer match.
_WEIGHT_ROLE = {"fact": 1.0, "session": 0.9, "user": 0.9, "skill": 0.85, "turn": 0.75, "assistant": 0.75}
# Chunked corpora produce many near-identical embeddings, so a single skill can fill
# every result slot on chunk multiplicity alone. Cap how many chunks one skill
# (conv_ref) may contribute; rows without a conv_ref (facts, turns) are never capped.
_MAX_CHUNKS_PER_SOURCE = int(_env_number("HERMES_ORACLE_MAX_CHUNKS_PER_SKILL", 2, allow_zero=True))
# Minimum candidates pulled per branch before post-fusion ranking. Must comfortably
# exceed top_k so role weighting and prefix boosting have material to re-rank.
_MIN_CANDIDATE_POOL = int(_env_number("HERMES_ORACLE_MIN_CANDIDATE_POOL", 50, lo=1))
_WEIGHT_TELEMETRY = _env_number("HERMES_ORACLE_TELEMETRY_WEIGHT", 0.05, hi=1.0, allow_zero=True)
_WEIGHT_SHORT_CHARS = 25
_WEIGHT_SHORT_FACTOR = 0.9


def _read_tuning(use_module_values: bool = False) -> dict[str, Any]:
    """The ranking and limit settings. Read from the settings of the active
    profile, or (use_module_values) the process-wide values read at import."""
    if use_module_values:
        return {
            "skill_fetch_max_chunks": _SKILL_FETCH_MAX_CHUNKS,
            "rerank_enabled": _RERANK_ENABLED,
            "recency_floor": _WEIGHT_RECENCY_FLOOR,
            "recency_scale_days": _WEIGHT_RECENCY_SCALE_DAYS,
            "max_chunks_per_source": _MAX_CHUNKS_PER_SOURCE,
            "min_candidate_pool": _MIN_CANDIDATE_POOL,
            "telemetry_weight": _WEIGHT_TELEMETRY,
        }
    return {
        "skill_fetch_max_chunks": int(_env_number("HERMES_ORACLE_SKILL_FETCH_MAX_CHUNKS", 400, lo=1)),
        "rerank_enabled": _setting("HERMES_ORACLE_DISABLE_RERANK").lower() not in ("1", "true", "yes"),
        "recency_floor": _env_number("HERMES_ORACLE_RECENCY_FLOOR", 0.85, hi=1.0, allow_zero=True),
        "recency_scale_days": _env_number("HERMES_ORACLE_RECENCY_SCALE_DAYS", 120.0),
        "max_chunks_per_source": int(_env_number("HERMES_ORACLE_MAX_CHUNKS_PER_SKILL", 2, allow_zero=True)),
        "min_candidate_pool": int(_env_number("HERMES_ORACLE_MIN_CANDIDATE_POOL", 50, lo=1)),
        "telemetry_weight": _env_number("HERMES_ORACLE_TELEMETRY_WEIGHT", 0.05, hi=1.0, allow_zero=True),
    }
_TELEMETRY_PREFIXES = ("[skill_usage]", "[skill_vectorization")


_SKILL_TAG_RE = re.compile(r"^\[skill_ingest\]\s*\[skill:([^\]]+)\]")


def _source_key(row: Any) -> str:
    """Identify the chunked source a row belongs to (a skill name), else "".

    Search projections do not always include conv_ref, so fall back to the
    `[skill_ingest] [skill:<name>]` prefix that sync_skills writes.
    """
    if not isinstance(row, dict):
        return ""
    ref = row.get("conv_ref")
    if ref:
        return str(ref)
    match = _SKILL_TAG_RE.match(str(row.get("content") or ""))
    return match.group(1) if match else ""


def _telemetry_sql(col: str = "NVL(content_txt, 'x')") -> str:
    """SQL fragment excluding lifecycle telemetry rows only.

    Derived from _TELEMETRY_PREFIXES so the SQL filter and the in-Python
    weighting pass (see _apply_weights) can never drift apart.
    """
    clauses = []
    for prefix in _TELEMETRY_PREFIXES:
        esc = (
            prefix.replace("\\", "\\\\")
            .replace("_", "\\_")
            .replace("%", "\\%")
        )
        clauses.append(f" AND {col} NOT LIKE '{esc}%' ESCAPE '\\'")
    return "".join(clauses)


_MAX_CHUNK_FILE_BYTES = 64 * 1024 * 1024
_CONTAINS_CLEAN_RE = re.compile(r"[^\w\s]")
_PREFIX_STOP_WORDS = {
    "a", "about", "after", "all", "also", "an", "and", "any", "are", "as", "at",
    "be", "been", "being", "but", "by", "can", "could", "did", "do", "does",
    "for", "from", "get", "got", "had", "has", "have", "he", "her", "him", "his",
    "how", "i", "if", "in", "into", "is", "it", "its", "just", "may", "me", "might",
    "more", "most", "must", "my", "no", "not", "now", "of", "on", "one", "only",
    "or", "our", "out", "over", "said", "say", "see", "shall", "she", "should",
    "so", "some", "such", "than", "that", "the", "their", "them", "then", "there",
    "these", "they", "this", "those", "through", "to", "too", "two", "under",
    "up", "us", "use", "used", "using", "very", "was", "way", "we", "were",
    "what", "when", "where", "which", "while", "who", "whom", "why", "will",
    "with", "would", "you", "your",
}
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
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only rows carrying these tags (case-insensitive), e.g. [\"handoff\", \"project:x\"].",
                },
                "tag_match": {
                    "type": "string",
                    "enum": ["all", "any"],
                    "description": "all (default) = row has every tag; any = at least one.",
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
            "that belong in Oracle. Accepts a single fact string, list of facts, or 'facts' array. "
            "Pass memory_id (e.g. handoff:<project>:<topic>) to write a stable, addressable row: an existing "
            "row with that id is updated in place (upsert). Pass tags to group rows for oracle_get."
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
                    "description": "Optional list of facts to store in batch (max 100 facts of 32,000 chars each; all or none are stored).",
                },
                "target": {
                    "type": "string",
                    "enum": ["memory", "user"],
                    "description": "memory = environment/conventions/projects; user = who they are/preferences.",
                },
                "memory_id": {
                    "type": "string",
                    "description": "Optional stable id (1-128 chars: letters, digits, _ . : @ / -). Upserts; single fact only.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags stored in metadata.tags (lower-cased), e.g. [\"handoff\", \"project:x\"].",
                },
            },
        },
    },
    {
        "name": "oracle_update",
        "description": "Replace the text and/or tags of an existing Oracle memory by memory_id (e.g. chk_* or fact_*, or a caller-chosen handoff id). Never use the local memory tool to edit Oracle entries.",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "content": {"type": "string", "description": "New text (optional when tags is given)."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Replace the row's tags with this list ([] clears them).",
                },
            },
            "required": ["memory_id"],
        },
    },
    {
        "name": "oracle_get",
        "description": (
            "Exact read of Oracle memories by memory_id(s) and/or tags, newest first. No embedding or "
            "vector search, so it works while the ONNX model is cooling down. Use it to pick up a handoff: "
            "oracle_get(memory_id='handoff:<project>:<topic>') or oracle_get(tags=['handoff','project:<name>'])."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "One exact memory_id."},
                "memory_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Several exact memory_ids (max 100).",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Rows carrying these tags (case-insensitive).",
                },
                "tag_match": {
                    "type": "string",
                    "enum": ["all", "any"],
                    "description": "all (default) = row has every tag; any = at least one.",
                },
                "session_id": {"type": "string", "description": "Restrict to a session ID."},
                "agent_id": {"type": "string", "description": "Restrict to an agent ID."},
                "target": {"type": "string", "enum": ["memory", "user"], "description": "Restrict to a target."},
                "limit": {"type": "integer", "description": "Max rows (default 20, max 100)."},
            },
        },
    },
    {
        "name": "oracle_delete",
        "description": "Delete an Oracle memory by memory_id (e.g. chk_* or fact_*) or purge an entire session thread by session_id from oracle_search (pass one, not both; skill-corpus rows and the shared ids 'skills'/'none' are never purged). Never use the local memory tool to remove Oracle entries.",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Specific memory ID to delete."},
                "session_id": {"type": "string", "description": "Session ID to delete all associated memories for thread purge."},
                "dry_run": {
                    "type": "boolean",
                    "description": "With session_id: report how many rows would be deleted and delete nothing.",
                },
                "max_rows": {
                    "type": "integer",
                    "description": (
                        f"With session_id: the most rows this call may delete (default {_PURGE_DEFAULT_MAX_ROWS}, "
                        f"max {_PURGE_HARD_MAX_ROWS}). A session with more rows is left untouched."
                    ),
                },
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
                    "description": "Chunk size in words, 10-1000 (default 80).",
                },
                "overlap": {
                    "type": "integer",
                    "description": "Word overlap, 0 to max_words/2 (default 10).",
                },
            },
        },
    },
    {
        "name": "oracle_sync_skills",
        "description": (
            "Scan and index this host's skills (the active profile's skills dir and ~/.agents/skills) into "
            "Oracle 26ai vector memory. Rows are stored under this host's identity; each changed skill is "
            "replaced in one transaction. prune=true also removes this host's skills that are no longer on "
            "disk (never other hosts' copies or the shared corpus); use dry_run=true to preview."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["meta", "full"],
                    "description": "meta (name+description+triggers) or full (chunk entire body). Default meta.",
                },
                "force": {
                    "type": "boolean",
                    "description": "Re-index even if already up to date. Default false.",
                },
                "dirs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional custom directories to scan for skills.",
                },
                "batch_size": {
                    "type": "integer",
                    "description": "Batch size for inserting rows into Oracle (default 50).",
                },
                "prune": {
                    "type": "boolean",
                    "description": "Also delete this host's skills that are no longer under the scanned dirs. Default false.",
                },
                "prune_limit": {
                    "type": "integer",
                    "description": "Maximum skills to prune in one call (1-500, default 100).",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Report what would be indexed/pruned without writing. Default false.",
                },
                "replace_foreign": {
                    "type": "boolean",
                    "description": (
                        "Also replace this host's copies stored from SKILL.md files outside this profile's "
                        "skill dirs (reported as skipped_foreign). Default false."
                    ),
                },
            },
        },
    },
    {
        "name": "oracle_load_skill",
        "description": (
            "Load a full skill from the shared Oracle skill corpus (works for skills NOT on local disk). "
            "'fetch' reconstructs the skill document from its chunks (word-accurate, line structure flattened): "
            "returns text + file manifest. 'list' returns name + description of every ingested skill (2,300+); "
            "supports substring filter and pagination."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["fetch", "list"], "default": "fetch"},
                "skill": {"type": "string", "description": "Skill name (exact, from list). Required for action=fetch."},
                "path": {
                    "type": "string",
                    "description": "action=fetch only: source path of one copy, when the skill exists in several (from the variants list).",
                },
                "variant": {
                    "type": "integer",
                    "description": "action=fetch only: variant number from the variants list, when the skill exists in several copies.",
                },
                "host": {
                    "type": "string",
                    "description": "action=fetch only: host of one copy (from the variants list), with or instead of 'path'.",
                },
                "filter": {"type": "string", "description": "action=list only: substring match on name/description."},
                "offset": {"type": "integer", "description": "action=list only: pagination offset (default 0)."},
            },
            "required": [],
        },
    },
]


def reassemble_chunks(chunks: list[str]) -> str:
    """Reconstruct original word sequence from overlapping word chunks.

    First chunk -> words; each subsequent chunk: find k = min(10, len(assembled), len(chunk))
    LARGEST with prev[-k:] == chunk[:k]; append chunk[k:]; if no k matches (chunk < 10 words
    or disjoint) append whole chunk. (Replicates chunk_markdown_text(max_words=80, overlap=10).)
    """
    if not chunks:
        return ""
    assembled_words: list[str] = []
    for chunk in chunks:
        text = chunk
        text = re.sub(r"^\[skill_ingest\]\s*\[skill:[^\]]+\](?:\s*[^\n]*(?:\(chunk \d+/\d+\)|\(reference:[^\)]+\) \(chunk \d+/\d+\)))?:\s*\n?", "", text)
        words = text.split()
        if not words:
            continue
        if not assembled_words:
            assembled_words.extend(words)
            continue

        matched_k = 0
        max_k = min(10, len(assembled_words), len(words))
        for k in range(max_k, 0, -1):
            if assembled_words[-k:] == words[:k]:
                matched_k = k
                break

        if matched_k > 0:
            assembled_words.extend(words[matched_k:])
        else:
            assembled_words.extend(words)

    return " ".join(assembled_words)


def _extract_skill_description(content: str, skill_name: str = "") -> str:
    """Extract clean skill description from content text.

    Correctly handles hyphenated skill names, em-dashes, en-dashes, hyphens,
    and strips trailing 'When to use:' trigger sections.
    """
    if not content:
        return ""
    text = re.sub(r"^\[skill_ingest\]\s*\[skill:[^\]]+\]\s*", "", content.strip())
    if skill_name:
        text = re.sub(rf"^{re.escape(skill_name)}\s*[—–-]?\s*", "", text.strip())
    else:
        text = re.sub(r"^[^\n—–]+[—–]\s*", "", text.strip())
    desc = text.splitlines()[0].strip() if text.splitlines() else ""
    if "When to use:" in desc:
        desc = desc.split("When to use:")[0].strip()
    return desc


def get_host_id() -> str:
    """This host's identity in skill-corpus ids (skill:<host>:<name>:c<N>).
    HERMES_HOST_ID when set; otherwise the short host name, lower-cased (a
    shell's HOSTNAME may be the FQDN while a systemd service sees none, and
    both must give the same id). Keep identical to scripts/sync_skills.py."""
    host_id = (os.environ.get("HERMES_HOST_ID") or "").strip()
    if not host_id:
        host_id = (os.environ.get("HOSTNAME") or "").strip()
        if not host_id:
            try:
                import socket

                host_id = socket.gethostname()
            except Exception:
                host_id = ""
        host_id = host_id.split(".")[0].lower()
    return re.sub(r"[^a-zA-Z0-9._-]", "", host_id) or "hermes"

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
    "If memory_id exists, use oracle_update(memory_id, ...). Use oracle_delete(memory_id) for obsolete or incorrect facts. "
    "oracle_add(memory_id=..., tags=[...]) writes a stable, addressable row (upsert); oracle_get(memory_id=... or tags=[...]) "
    "reads rows back exactly, without vector search.\n\n"
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
    "9. VALIDATION BEFORE WRITE: Confirm durability, dedupe via search, keep content compact, normalize terminology.\n\n"
    "10. HANDOFFS: To hand work to another agent or session, oracle_add(memory_id=\"handoff:<project>:<topic>\", "
    "tags=[\"handoff\", \"project:<name>\"], content=<state, next steps, blockers>). The receiver calls "
    "oracle_get(tags=[\"handoff\", \"project:<name>\"]) (or the exact memory_id), then oracle_update or oracle_delete it."
)


def _json_default(o: Any) -> Any:
    """Coerce Oracle JSON/NUMBER values (Decimal, dates) into JSON-safe types."""
    if isinstance(o, decimal.Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


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


def _load_module_from_path(name: str, path: Path):
    """Import a .py file by path, registering it in sys.modules first.

    dataclasses/pickle resolve cls.__module__ through sys.modules, so the
    module must be registered *before* exec_module() runs.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


class OracleMemoryProvider(MemoryProvider):
    """Oracle Autonomous AI Database 26ai VECTOR + Text + JSON memory."""

    def __init__(self) -> None:
        self._pool: Any = None
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
        self._embed_attempts: dict[str, int] = {}
        self._embed_worker: threading.Thread | None = None
        self._backfill_scan_thread: threading.Thread | None = None
        self._pool_lock = threading.RLock()
        self._onnx_failed_at: float | None = None
        self._lock = threading.Lock()
        self._write_queue: queue.Queue = queue.Queue()
        self._write_worker: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._write_stop = threading.Event()
        self._init_error = ""
        self._agent_context = "primary"
        self._initialized = False
        self._pool_fingerprint = ""
        self._last_write_at: str | None = None
        self._bg_drop_warned = False
        # Lifecycle writers are tracked per process (module-level
        # _LIFECYCLE_* maps, keyed by _lifecycle_key), not per instance.
        self._sync_threads: list[threading.Thread] = []
        self._sync_threads_lock = threading.Lock()
        self._table = "hermes_agent_memory"
        self._onnx_model = "BGE_BASE_EN_V1_5"
        # Bound in initialize() from the session's profile (never process-wide):
        # the profile home, its timeouts and its schema policy. None means
        # "not initialized": the module-level defaults apply.
        self._hermes_home: Path | None = None
        self._search_timeout_ms: int | None = None
        self._ingest_timeout_ms: int | None = None
        self._schema_policy: str | None = None
        self._tuning: dict[str, Any] | None = None
        # Set by _verify_schema: what the schema lacks, and the vector index
        # found: "HNSW", "IVF", "none", or "unknown" (not read yet, the table
        # belongs to another schema, or a type this version does not know).
        self._schema_warnings: list[str] = []
        self._vector_index_kind = "unknown"

    @property
    def name(self) -> str:
        return "oracle"

    # This provider's own budgets and ranking settings. initialize() reads
    # them inside the profile's scope; before that the process-wide defaults
    # apply.
    @property
    def _search_ms(self) -> int:
        return _SEARCH_TIMEOUT_MS if self._search_timeout_ms is None else self._search_timeout_ms

    @property
    def _search_secs(self) -> float:
        return self._search_ms / 1000.0

    @property
    def _ingest_ms(self) -> int:
        return _INGEST_TIMEOUT_MS if self._ingest_timeout_ms is None else self._ingest_timeout_ms

    def _tune(self, key: str) -> Any:
        """One ranking or limit setting of this provider's profile."""
        if self._tuning is not None:
            return self._tuning[key]
        return _read_tuning(use_module_values=True)[key]

    def health(self) -> dict[str, Any]:
        """Return health status of provider: pool, rows (capped), last_write,
        init_error, schema_warnings (missing indexes found at start-up),
        vector_index (HNSW, IVF, none or unknown)."""
        rows_count = 0
        if self._pool:
            try:
                with self._pool.acquire() as conn:
                    try:
                        conn.call_timeout = self._search_ms
                    except Exception:
                        pass
                    with conn.cursor() as cur:
                        cur.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM {self._table} FETCH FIRST 10001 ROWS ONLY)")
                        row = cur.fetchone()
                        if row:
                            rows_count = int(row[0])
            except Exception:
                pass
        return {
            "pool": bool(self._pool is not None),
            "rows": rows_count,
            "last_write": self._last_write_at or "none",
            "init_error": self._init_error or None,
            "schema_warnings": list(self._schema_warnings),
            "vector_index": self._vector_index_kind,
        }

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
        # Hermes passes the profile-scoped home; never assume ~/.hermes.
        self._hermes_home = _hermes_home_path(kwargs.get("hermes_home"))
        self._schema_policy = _setting("HERMES_ORACLE_SCHEMA_POLICY").upper()
        self._tuning = _read_tuning()

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
        # One read of every timeout, inside the profile's scope. This provider
        # keeps its own values; the module-level ones are refreshed as well and
        # serve only providers that were never initialized.
        global _SEARCH_TIMEOUT_MS, _SEARCH_TIMEOUT_SECS, _INGEST_TIMEOUT_SECS, _INGEST_TIMEOUT_MS
        search_timeout_ms = _search_timeout_ms()
        ingest_timeout_ms = int(_ingest_timeout_secs() * 1000)
        self._search_timeout_ms, self._ingest_timeout_ms = search_timeout_ms, ingest_timeout_ms
        _SEARCH_TIMEOUT_MS, _SEARCH_TIMEOUT_SECS = search_timeout_ms, search_timeout_ms / 1000.0
        _INGEST_TIMEOUT_MS, _INGEST_TIMEOUT_SECS = ingest_timeout_ms, ingest_timeout_ms / 1000.0
        pool_wait_ms = _pool_wait_timeout_ms()
        pool_idle_s = _pool_idle_timeout_secs()
        pool_call_timeout = max(search_timeout_ms, ingest_timeout_ms)
        # Timeout config participates in the fingerprint: a long-lived app
        # process must re-create its pool when these env vars change,
        # otherwise it silently keeps serving connections with the old
        # call_timeout budget (stale 13s clamp -> DPY-4024 on writes).
        current_fingerprint = (
            f"{self._user}@{self._dsn}:{self._table}"
            f":ct={pool_call_timeout}:wt={pool_wait_ms}:it={pool_idle_s}"
        )

        with self._pool_lock:
            self._initialize_pool_locked(password, current_fingerprint, pool_call_timeout, pool_wait_ms, pool_idle_s)

    def _find_wallet_dir(self) -> str:
        """The wallet directory: OCI_WALLET_LOCATION, then TNS_ADMIN (both from
        the profile's settings), then the first of ~/.oci/wallet and
        <Hermes home>/oracle_wallet that holds a wallet. "" when there is none."""
        for name in ("OCI_WALLET_LOCATION", "TNS_ADMIN"):
            value = _setting(name)
            if value:
                return value
        candidates: list[Path] = []
        try:
            candidates.append(Path.home() / ".oci" / "wallet")
        except (OSError, RuntimeError, KeyError):
            pass
        home = self._hermes_home or _hermes_home_path()
        if home is not None:
            candidates.append(home / "oracle_wallet")
        for candidate in candidates:
            try:
                if (candidate / "cwallet.sso").exists() or (candidate / "tnsnames.ora").exists():
                    return str(candidate)
            except OSError:
                continue
        return ""

    def _initialize_pool_locked(
        self, password: str, current_fingerprint: str, pool_call_timeout: int, pool_wait_ms: int, pool_idle_s: int
    ) -> None:
        """Reuse, or (re)build, the session pool. Runs under _pool_lock so a
        deferred teardown (_finish_shutdown_when_idle) cannot close a pool that
        this call is re-arming, and a rebuild always retires the pool it replaces."""
        # Idempotent initialization: reuse existing pool if configuration matches
        if self._pool is not None and self._initialized and self._pool_fingerprint == current_fingerprint:
            # Re-arm after a "busy" shutdown(), which returns early with the pool
            # still open but the writer stopped and the provider unregistered.
            self._write_stop.clear()
            _register_live_provider(self)
            logger.debug("Oracle memory pool already initialized with matching config; reusing pool.")
            return

        # A changed fingerprint (credentials, DSN, table or timeouts) replaces the
        # pool; the old one is closed below instead of being left open.
        old_pool: Any = self._pool
        new_pool: Any = None
        try:
            import oracledb

            # Timeout config is read once (below the pool-reuse check above) so the
            # fingerprint and the actual pool construction can never disagree — an
            # env change observed mid-initialize is resolved at a single point.
            pool_kwargs: dict[str, Any] = {
                "user": self._user,
                "password": password,
                "dsn": self._dsn,
                "min": 2,
                "max": 6,
                "increment": 1,
                "threaded": True,
                # TIMEDWAIT: acquire() raises DPY-4005 after wait_timeout (ms)
                # instead of blocking forever when all sessions are busy.
                "getmode": oracledb.POOL_GETMODE_TIMEDWAIT,
                "wait_timeout": pool_wait_ms,
                "timeout": pool_idle_s,  # seconds idle before extra sessions close
                "call_timeout": pool_call_timeout,
            }

            wallet_dir = self._find_wallet_dir()

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

            # Captured by value: the callback must not hold the provider, or the
            # pool and the provider keep each other alive after a dropped agent.
            session_call_timeout = max(self._search_ms, self._ingest_ms)

            def _session_callback(conn: Any, tag: Any = None) -> None:
                try:
                    # Default per-connection budget must cover writes: bulk
                    # inserts (_insert_many) acquire a fresh pool connection and
                    # were being clamped to the 13s search default -> DPY-4024.
                    # Read paths that want a tight fail-fast window set
                    # conn.call_timeout to the search budget themselves after
                    # acquire (health, search, skill fetch, list).
                    conn.call_timeout = session_call_timeout
                except Exception:
                    pass
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

            new_pool = _CallTimeoutPool(oracledb.SessionPool(**pool_kwargs), pool_call_timeout)
            self._pool = new_pool
            self._initialized = True
            self._pool_fingerprint = current_fingerprint
            self._write_stop.clear()
            self._ensure_schema()
            # Register only once ready, so hooks never route to a half-built provider.
            _register_live_provider(self)
            self._init_error = ""
            logger.info("Oracle 26ai memory pool ready (session=%s)", self._session_id)
            if old_pool is not None and old_pool is not new_pool:
                _retire_pool(old_pool, "replaced")
                old_pool = None
            self._start_backfill_scan()
        except Exception as e:
            err_msg = str(e)
            if password and password in err_msg:
                err_msg = err_msg.replace(password, "******")
            self._init_error = err_msg
            logger.error("Oracle memory pool failed: %s", err_msg)
            # Not ready: never look initialized, never receive hook/handoff
            # writes, and do not leak the sessions of a pool built in this call.
            self._pool = None
            self._initialized = False
            self._pool_fingerprint = ""
            with _LIVE_PROVIDERS_LOCK:
                _LIVE_PROVIDERS.discard(self)
            if new_pool is not None:
                try:
                    new_pool.close(force=True)
                except Exception as close_e:
                    logger.debug("oracle pool close after failed init: %s", close_e)
            if old_pool is not None and old_pool is not new_pool:
                # The provider is unusable until the next successful initialize;
                # never leak the previous configuration's sessions meanwhile.
                _retire_pool(old_pool, "replaced")

    def _ensure_schema(self) -> None:
        if not self._pool:
            return
        # The profile's policy as read in initialize(); a direct call reads it now.
        policy = self._schema_policy
        if policy is None:
            policy = _setting("HERMES_ORACLE_SCHEMA_POLICY").upper()
        if policy == "REQUIRE_EXISTING":
            logger.info("HERMES_ORACLE_SCHEMA_POLICY=REQUIRE_EXISTING set: skipping runtime DDL and verifying schema")
            self._verify_schema()
            return
        # self._table is sanitized to [A-Za-z0-9_] in initialize(), so it is
        # safe to interpolate (DDL takes no bind variables).
        table = self._table.upper()
        text_index, vector_index = f"{table}_TXT_IDX", f"{table}_VEC_IDX"
        failures: dict[str, str] = {}
        with self._pool.acquire() as conn, conn.cursor() as cur:
            err = self._run_ddl(cur, TABLE_DDL.format(table_name=table))
            if err:
                logger.warning("Oracle memory schema: creating table %s failed: %s", table, err)
            self._drop_failed_text_index(cur, text_index)
            steps = [
                ("text index", TEXT_INDEX_DDL.format(index_name=text_index, table_name=table)),
                ("vector index", VECTOR_INDEX_DDL.format(index_name=vector_index, table_name=table)),
            ]
            steps += [
                (f"column {column}", f"ALTER TABLE {table} ADD ({column} {definition})")
                for column, definition in _UPGRADE_COLUMNS
            ]
            for what, sql in steps:
                err = self._run_ddl(cur, sql)
                if err:
                    failures[what] = err
        self._verify_schema(failures)

    @staticmethod
    def _run_ddl(cur: Any, sql: str) -> str:
        """Run one bootstrap statement. Returns "" when it ran or the object
        is already there (the normal case on every start but the first), and
        the first line of the error otherwise."""
        try:
            cur.execute(sql)
            return ""
        except Exception as e:
            first = (str(e).strip().splitlines() or [type(e).__name__])[0]
            if any(code in first for code in _DDL_ALREADY_EXISTS):
                return ""
            return first

    def _index_rows(self, cur: Any) -> list[tuple]:
        """(index_name, index_type, index_subtype, ityp_name, domidx_opstatus,
        column_name) for the table's indexes; [] when the dictionary has none
        (the table belongs to another schema) or cannot be read."""
        try:
            cur.execute(_INDEX_STATE_SQL, [self._table])
            return [tuple(r) for r in cur.fetchall() if isinstance(r, (tuple, list)) and len(r) == 6]
        except Exception as e:
            logger.debug("oracle schema: index lookup skipped: %s", e)
            return []

    @staticmethod
    def _text_indexes(rows: list[tuple]) -> list[tuple]:
        """Oracle Text indexes on CONTENT (the JSON search index on METADATA
        has the same index type and is not one of them)."""
        return [
            r for r in rows
            if r[1] == "DOMAIN" and str(r[3] or "").upper().startswith("CONTEXT") and str(r[5] or "").upper() == "CONTENT"
        ]

    def _drop_failed_text_index(self, cur: Any, index_name: str) -> None:
        """1.1.1 to 1.1.1.5 created the text index FOR JSON on the CLOB column.
        That statement fails and leaves a FAILED domain index, which rejects
        every INSERT (ORA-29861). Drop it so it can be created again. Only the
        index this bootstrap names is touched, and only in the FAILED state."""
        for row in self._text_indexes(self._index_rows(cur)):
            if str(row[0]).upper() == index_name.upper() and str(row[4] or "").upper() == "FAILED":
                logger.warning(
                    "Oracle memory schema: text index %s is marked FAILED (left by an earlier bootstrap); "
                    "dropping it and creating it again", index_name,
                )
                try:
                    cur.execute(f"DROP INDEX {index_name} FORCE")
                except Exception as e:
                    logger.warning("Oracle memory schema: dropping %s failed: %s", index_name, e)

    def _verify_schema(self, failures: dict[str, str] | None = None) -> None:
        """Raise if the memory table is not usable, so `initialize` surfaces
        the failure instead of reporting the provider as active while broken:
        the table must be queryable, and a text index in the FAILED state is
        fatal because it rejects every write. A missing text or vector index
        only degrades search, so it is logged and kept for health().
        *failures* maps a bootstrap step to the error it ended with."""
        table = self._table.upper()
        failures = dict(failures or {})
        with self._pool.acquire() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT /*+ NO_PARALLEL */ COUNT(*) FROM {self._table}")  # nosec B608
            cur.fetchone()
            rows = self._index_rows(cur)
        warnings: list[str] = []
        kind = "unknown"
        if rows:  # none: not this user's table (a synonym), so nothing to judge
            text = self._text_indexes(rows)
            failed = [str(r[0]) for r in text if str(r[4] or "").upper() == "FAILED"]
            if failed:
                raise RuntimeError(
                    f"text index {failed[0]} on {table}.CONTENT is marked FAILED, so no row can be written "
                    f"(ORA-29861). Drop it and create it again: DROP INDEX {failed[0]} FORCE; "
                    + TEXT_INDEX_DDL.format(index_name=f"{table}_TXT_IDX", table_name=table)
                )
            if not text:
                why = failures.pop("text index", "")
                warnings.append(
                    f"no Oracle Text index on {table}.CONTENT"
                    + (f" (creating it failed: {why})" if why else "")
                    + ": text search fails and hybrid search runs on the vector leg only. Create it: "
                    + TEXT_INDEX_DDL.format(index_name=f"{table}_TXT_IDX", table_name=table)
                )
            vector = [r for r in rows if r[1] == "VECTOR"]
            if vector:
                subtype = str(vector[0][2] or "").upper()
                kind = "HNSW" if "HNSW" in subtype else "IVF" if "IVF" in subtype else "unknown"
            else:
                kind = "none"
                why = failures.pop("vector index", "")
                warnings.append(
                    f"no vector index on {table}.EMBEDDING"
                    + (f" (creating it failed: {why})" if why else "")
                    + ": vector search compares every row"
                )
        warnings += [f"creating the {what} failed: {err}" for what, err in failures.items()]
        for w in warnings:
            # Hermes builds a provider per agent and per API session; say it
            # once per process, and keep it in health() for every instance.
            seen = (self._pool_fingerprint, w)
            level = logging.DEBUG if seen in _SCHEMA_WARNINGS_LOGGED else logging.WARNING
            _SCHEMA_WARNINGS_LOGGED.add(seen)
            logger.log(level, "Oracle memory schema: %s", w)
        self._schema_warnings = warnings
        self._vector_index_kind = kind

    def system_prompt_block(self) -> str:
        if not self._pool:
            reason = self._init_error or self.unavailable_reason() or "not initialized"
            return f"# Oracle 26ai Memory\nUnavailable ({reason}). No memory store is available. Local memory is prohibited."
        return (
            "# Oracle 26ai Memory (Primary Store)\n"
            f"Active: hybrid search (vector + Oracle Text). Agent: {self._agent_id}.\n"
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

    @staticmethod
    def _safe_embed_text(text: str | None, max_chars: int = 1200) -> str:
        """Clamp text length to safely fit within ONNX model's 512-token limit.

        Dense inputs (UUIDs, JSON, code, URLs) can exceed 512 tokens if 4000 characters
        are passed to VECTOR_EMBEDDING, causing ORA-54401 ONNX broadcast errors.
        1200 characters safely guarantees token count stays below the 512 sequence limit.
        """
        if not text:
            return ""
        return text[:max_chars]

    @staticmethod
    def _safe_varchar_bytes(text: str | None, max_bytes: int = 4000) -> str:
        """Clamp text to fit within max_bytes in UTF-8 encoding.

        Prevents ORA-12899: value too large for column when inserting into VARCHAR2(4000).
        """
        if not text:
            return ""
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    def _onnx_in_cooldown(self) -> bool:
        with self._embed_lock:
            failed_at = self._onnx_failed_at
        return failed_at is not None and (
            time.monotonic() - failed_at < _ONNX_RETRY_AFTER_SECS
        )

    def _onnx_cooldown_remaining(self) -> float:
        with self._embed_lock:
            failed_at = self._onnx_failed_at
        if failed_at is None:
            return 0.0
        return max(0.0, _ONNX_RETRY_AFTER_SECS - (time.monotonic() - failed_at))

    def _onnx_mark_failed(self, err: Any = None) -> None:
        if not _is_onnx_model_failure(err):
            logger.info("ONNX cooldown NOT tripped (session/driver error, not a model failure): %s", err)
            return
        with self._embed_lock:
            self._onnx_failed_at = time.monotonic()

    @staticmethod
    def _is_onnx_transient(err_str: str) -> bool:
        """True when an embedding-insert error can safely degrade to a
        NULL-embedding insert + background backfill instead of hard-failing.

        ORA-51815 / ORA-54401 are already handled by their dedicated retry
        branches. This covers *other* server-side transient conditions (task
        killed, PGA OOM, parallelism limit) where the row itself is valid.
        Real data bugs — unique constraint, value-too-large, invalid number
        — MUST hard-fail, so they are excluded from the allowlist.
        """
        s = err_str.lstrip()
        return s.startswith(_ONNX_TRANSIENT_PREFIXES)

    def _onnx_mark_ok(self) -> None:
        with self._embed_lock:
            self._onnx_failed_at = None

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
                        binds.append(self._safe_embed_text(t))
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
            err_str = str(e)
            if "ORA-54401" in err_str:
                logger.info(
                    "Retrying in-database ONNX embed_many with conservative token clamp (%d chars)",
                    _CONSERVATIVE_EMBED_CHARS,
                )
                try:
                    db_vecs = [None] * len(miss_texts)
                    with self._pool.acquire() as conn, conn.cursor() as cur:
                        for i in range(0, len(miss_texts), _EMBED_BATCH):
                            batch_texts = miss_texts[i : i + _EMBED_BATCH]
                            parts = []
                            binds = []
                            for j, t in enumerate(batch_texts):
                                parts.append(
                                    f"SELECT /*+ NO_PARALLEL */ {j} AS id, VECTOR_EMBEDDING({self._onnx_model} USING :{j + 1} AS DATA) AS vec FROM DUAL"
                                )
                                binds.append(self._safe_embed_text(t, _CONSERVATIVE_EMBED_CHARS))
                            cur.execute(" UNION ALL ".join(parts) + " ORDER BY id", binds)
                            for row in cur.fetchall():
                                idx = int(row[0])
                                vec = row[1] if len(row) > 1 else None
                                if vec is not None and len(vec) == _EMBED_DIM:
                                    db_vecs[i + idx] = vec
                    if any(v is not None for v in db_vecs):
                        got = db_vecs
                        got_tags = [
                            self._onnx_model if v is not None else None for v in got
                        ]
                        self._onnx_mark_ok()
                        for text, vec, model in zip(miss_texts, got, got_tags):
                            if vec is not None:
                                self._cache_put(text, vec, model)
                            for i in unique_indices[text]:
                                cached[i] = (vec, model)
                        return [c if c is not None else (None, None) for c in cached]
                except Exception as retry_e:
                    err_str = str(retry_e)

            is_shape_err = any(code in err_str for code in ("ORA-54401", "ORA-51815"))
            if not is_shape_err:
                self._onnx_mark_failed(err_str)
            logger.warning(
                "In-database ONNX embed_many failed (%s); external fallback is disabled",
                err_str,
            )
            raise RuntimeError(
                f"In-database ONNX embedding failed ({err_str}); external fallback is disabled"
            ) from e

        for text, vec, model in zip(miss_texts, got, got_tags):
            if vec is not None:
                self._cache_put(text, vec, model)
            for i in unique_indices[text]:
                cached[i] = (vec, model)
        return [c if c is not None else (None, None) for c in cached]

    def _embed(self, text: str) -> array.array | None:
        """One text through the in-database model (the vector only). Used by
        tests/verify_provider.py to check that the model answers."""
        if not text:
            return None
        return self._embed_many_tagged([text])[0][0]

    def _queue_embed_backfill(self, memory_id: str, content: str) -> None:
        """Queue a row for batched background embedding.

        Writes stay off the network. Text CONTAINS is live immediately; the
        vector index catches up in batches of _EMBED_BATCH.
        """
        if not content or not memory_id:
            return
        with self._embed_lock:
            # One pending entry per row, carrying the latest content: a row
            # updated twice before its backfill must be embedded from the
            # newer text, not the first one queued.
            for i, (mid, _) in enumerate(self._embed_pending):
                if mid == memory_id:
                    self._embed_pending[i] = (memory_id, content)
                    break
            else:
                self._embed_pending.append((memory_id, content))
        self._ensure_embed_worker()

    def _ensure_embed_worker(self) -> threading.Thread | None:
        """Start the backfill worker when rows are pending and none is running.
        The check and the start happen under _embed_lock, so two callers can
        never start two workers."""
        with self._embed_lock:
            if not self._embed_pending:
                return self._embed_worker
            if self._embed_worker is not None and self._embed_worker.is_alive():
                return self._embed_worker
            t = _spawn_thread(self._embed_worker_loop, name="oracle-embed-backfill")
            self._embed_worker = t
            t.start()
            return t

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
        if self._pool is None or self._write_stop.is_set():
            # Not initialized, or already shut down: the worker could only fail
            # this row with "pool not initialized". Hand it to a live provider
            # in this process if there is one; otherwise drop it loudly once.
            live = self._live_provider_for(session_id) if self._initialized else None
            if live is not None and live is not self:
                live._queue_background_insert(
                    memory_id=memory_id,
                    session_id=session_id,
                    role=role,
                    content=content,
                    target=target,
                    metadata=metadata,
                )
                return
            reason = "provider shut down" if self._initialized else "provider not initialized"
            if not self._bg_drop_warned:
                self._bg_drop_warned = True
                logger.warning("oracle background insert dropped: %s", reason)
            else:
                logger.debug("oracle background insert dropped: %s", reason)
            return
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
            self._write_worker = _spawn_thread(self._write_worker_loop, name="oracle-write-worker")
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

    # Text up to this many UTF-8 bytes is bound as a plain string (it converts
    # to CLOB in DBMS_LOB.COMPARE on any database); longer text goes in as a
    # temporary CLOB, since a string bind over 32,767 bytes fails in SQL
    # (ORA-01460) even with a CLOB input size.
    _BACKFILL_STR_BIND_MAX_BYTES = 4000

    def _backfill_write(self, conn: Any, todo: list[tuple[Any, Any, str, str]]) -> list[str]:
        """Write the vectors in *todo* ((vec, model, memory_id, text) tuples) and
        return the ids whose UPDATE matched no row (every id of a group whose
        per-row counts the driver cannot report, so the caller re-checks them
        instead of dropping a mismatch silently). The vector is written only
        for the text it was computed from, and only while the row still has
        none: a row updated meanwhile (here or on another host) keeps its newer
        content's embedding."""
        import oracledb

        sql = (
            f"UPDATE /*+ NO_PARALLEL */ {self._table} SET embedding = :1, "
            "embedding_model = :2, embedding_updated_at = SYSTIMESTAMP "
            "WHERE memory_id = :3 AND embedding IS NULL "
            "AND DBMS_LOB.COMPARE(content, :4) = 0"
        )  # nosec B608
        limit = self._BACKFILL_STR_BIND_MAX_BYTES
        small = [t for t in todo if len(t[3].encode("utf-8")) <= limit]
        big = [t for t in todo if len(t[3].encode("utf-8")) > limit]
        unmatched: list[str] = []
        for group in (small, big):
            if not group:
                continue
            if group is big:
                binds: list[tuple[Any, ...]] = [
                    (vec, model, mid, conn.createlob(oracledb.DB_TYPE_CLOB, text)) for vec, model, mid, text in group
                ]
            else:
                binds = [tuple(t) for t in group]
            with conn.cursor() as cur:
                cur.executemany(sql, binds, arraydmlrowcounts=True)
                try:
                    counts = list(cur.getarraydmlrowcounts())
                except Exception:
                    counts = []
            # Drop the temporary LOBs now: the driver frees them on the
            # connection's next round trip (the commit).
            del binds
            if len(counts) == len(group):
                unmatched.extend(t[2] for t, c in zip(group, counts) if not c)
            else:
                unmatched.extend(t[2] for t in group)
        return unmatched

    def _backfill_reread(self, conn: Any, mids: list[str]) -> dict[str, str]:
        """Current text of the rows among *mids* that still have no embedding."""
        out: dict[str, str] = {}
        for i in range(0, len(mids), 100):
            part = mids[i : i + 100]
            marks = ", ".join(f":{n + 1}" for n in range(len(part)))
            with conn.cursor() as cur:
                _execute_lobs_as_str(
                    cur,
                    f"SELECT memory_id, content FROM {self._table} "  # nosec B608
                    f"WHERE memory_id IN ({marks}) AND embedding IS NULL",
                    part,
                )
                for mid, content in cur.fetchall() or []:
                    text = _lob_text(content) if not isinstance(content, str) else content
                    if mid and text:
                        out[str(mid)] = text
        return out

    def _embed_worker_loop(self) -> None:
        """Embed queued NULL-embedding rows in batches.

        A failed batch is re-queued with exponential backoff
        (_BACKFILL_BASE_DELAY_SECS doubling up to _BACKFILL_MAX_DELAY_SECS); a
        row that failed _BACKFILL_MAX_ATTEMPTS times leaves the in-memory queue
        (it keeps its NULL embedding and the next startup re-queues it). During
        an ONNX cooldown the worker waits for the cooldown to end instead of
        burning attempts. After shutdown() has begun each pending batch gets one
        attempt (no backoff), so teardown drains what it can within its join
        budget; nothing is written to local files."""
        fails = 0
        while True:
            with self._embed_lock:
                if not self._embed_pending:
                    if self._embed_worker is threading.current_thread():
                        self._embed_worker = None
                    return
                stopping = self._write_stop.is_set()
                cooling = self._onnx_failed_at is not None and (
                    time.monotonic() - self._onnx_failed_at < _ONNX_RETRY_AFTER_SECS
                )
                if self._pool is None or (stopping and cooling):
                    left = len(self._embed_pending)
                    self._embed_pending.clear()
                    self._embed_attempts.clear()
                    if self._embed_worker is threading.current_thread():
                        self._embed_worker = None
                    logger.info(
                        "embed backfill: %d row(s) keep NULL embeddings (%s); the next startup re-queues them",
                        left,
                        "provider shut down" if self._pool is None else "ONNX cooldown at shutdown",
                    )
                    return
                batch: list[tuple[str, str]] = []
                if not cooling:
                    batch = [self._embed_pending.popleft() for _ in range(min(_EMBED_BATCH, len(self._embed_pending)))]
            if cooling:
                self._write_stop.wait(self._onnx_cooldown_remaining() + 0.5)
                continue
            pool = self._pool
            done: set[str] = set()
            requeued: set[str] = set()  # unmatched rows queued again with their stored text
            try:
                if pool is None:
                    raise RuntimeError("pool not initialized")
                tagged = self._embed_many_tagged([c for _, c in batch])
                todo = [
                    (vec, model, mid, text)
                    for (mid, text), (vec, model) in zip(batch, tagged)
                    if vec is not None
                ]
                if todo:
                    with pool.acquire() as conn:
                        unmatched = self._backfill_write(conn, todo)
                        conn.commit()
                        # Rows not confirmed written: done if they got an
                        # embedding meanwhile or are gone; otherwise re-queued
                        # once more with the text the database holds now. Not
                        # during shutdown: the drain must not spend its join
                        # budget on retries (the next startup re-queues them).
                        recheck = bool(unmatched) and not self._write_stop.is_set()
                        reread = self._backfill_reread(conn, unmatched) if recheck else {}
                    for mid, text in reread.items():
                        with self._embed_lock:
                            if any(m == mid for m, _ in self._embed_pending):
                                continue  # a newer write already queued it
                            n = self._embed_attempts.get(mid, 0) + 1
                            if n >= _BACKFILL_MAX_ATTEMPTS:
                                self._embed_attempts.pop(mid, None)
                                logger.warning(
                                    "embed backfill: %s never matched its stored text; it keeps a NULL "
                                    "embedding until the next startup", mid,
                                )
                                continue
                            self._embed_attempts[mid] = n
                            self._embed_pending.append((mid, text))
                            requeued.add(mid)
                    if unmatched and not recheck:
                        logger.info(
                            "embed backfill: %d row(s) not confirmed written; not re-checked during shutdown "
                            "(any still without an embedding is re-queued at the next startup)", len(unmatched),
                        )
                    elif unmatched:
                        logger.info(
                            "embed backfill: %d row(s) not confirmed written (changed or embedded meanwhile, or "
                            "no per-row counts); %d re-queued with their stored text", len(unmatched), len(reread),
                        )
                done = {mid for _, _, mid, _ in todo}
            except Exception as e:
                logger.warning("embed backfill batch failed: %s", e)
            remaining = [item for item in batch if item[0] not in done]
            dropped = 0
            keep: list[tuple[str, str]] = []
            with self._embed_lock:
                for mid in done - requeued:
                    self._embed_attempts.pop(mid, None)
                stopping = self._write_stop.is_set()
                for mid, content in remaining:
                    n = self._embed_attempts.get(mid, 0) + 1
                    if stopping or n >= _BACKFILL_MAX_ATTEMPTS:
                        self._embed_attempts.pop(mid, None)
                        dropped += 1
                    else:
                        self._embed_attempts[mid] = n
                        keep.append((mid, content))
                self._embed_pending.extendleft(reversed(keep))
            if dropped:
                logger.warning(
                    "embed backfill: %d row(s) left with NULL embeddings after %s; the next startup re-queues them",
                    dropped,
                    "shutdown" if stopping else f"{_BACKFILL_MAX_ATTEMPTS} attempts",
                )
            if not remaining:
                fails = 0
                continue
            fails += 1
            if keep:
                delay = min(_BACKFILL_BASE_DELAY_SECS * (2 ** (fails - 1)), _BACKFILL_MAX_DELAY_SECS)
                logger.warning(
                    "embed backfill: %d/%d rows still missing embeddings; retrying in %.0fs",
                    len(keep),
                    len(batch),
                    delay,
                )
                self._write_stop.wait(delay)

    def _requeue_null_embeddings(self, limit: int = _BACKFILL_STARTUP_LIMIT) -> int:
        """Queue rows that still have a NULL embedding (left by a cooldown, a
        transient error or a shutdown that could not drain the backfill).
        Read-only apart from the backfill UPDATEs it schedules. Returns the
        number of rows queued."""
        pool = self._pool
        if pool is None or not self._onnx_model:
            return 0
        sql = f"""
            SELECT memory_id, content FROM {self._table}
            WHERE embedding IS NULL AND content IS NOT NULL
            ORDER BY created_at DESC
            FETCH FIRST :n ROWS ONLY
        """  # nosec B608
        with self._acquire_checked() as conn:
            try:
                conn.call_timeout = self._search_ms
            except Exception:
                pass
            with conn.cursor() as cur:
                cur.arraysize = max(1, min(int(limit), 1000))
                _execute_lobs_as_str(cur, sql, {"n": int(limit)})
                rows = cur.fetchall() or []
        queued = 0
        for row in rows:
            mid, content = row[0], _lob_text(row[1])
            if mid and content:
                self._queue_embed_backfill(str(mid), content)
                queued += 1
        if queued:
            logger.info("embed backfill: re-queued %d row(s) with NULL embeddings at startup", queued)
        return queued

    def _start_backfill_scan(self) -> None:
        """Once per pool configuration per process, re-queue NULL-embedding rows
        in the background (primary agents only; never blocks initialize())."""
        if self._agent_context != "primary" or not self._onnx_model:
            return
        key = self._pool_fingerprint
        with _BACKFILL_SCANNED_LOCK:
            if key in _BACKFILL_SCANNED:
                return
            _BACKFILL_SCANNED.add(key)

        def _run() -> None:
            try:
                self._requeue_null_embeddings()
            except Exception as e:
                logger.info("embed backfill startup scan skipped: %s", e)

        t = _spawn_thread(_run, name="oracle-backfill-scan")
        self._backfill_scan_thread = t
        t.start()

    def _contains_query(self, query: str) -> str:
        cleaned = _CONTAINS_CLEAN_RE.sub(" ", query or "")
        tokens = [t for t in cleaned.split() if len(t) > 2][:8]
        if not tokens:
            return "{memory}"
        # ACCUM ranks a union of terms; AND of every token misses too often.
        escaped = [f"{{{t}}}" for t in tokens]
        return " ACCUM ".join(escaped)

    # -- writes / reads ------------------------------------------------------

    _INSERT_COLUMNS = (
        "memory_id, session_id, agent_id, role, content, embedding, created_at, "
        "target, metadata, content_txt, embedding_model, embedding_updated_at"
    )
    _INSERT_ROW_COLUMNS = ", turn_index, chunk_index, turn_id, conv_ref"

    def _insert_sql(self, *, embed: bool, row_columns: bool) -> str:
        """The INSERT for one row shape.

        embed: the database computes the embedding from a bound text;
        otherwise the embedding is NULL and the row is embedded later.
        row_columns: also write turn_index, chunk_index, turn_id, conv_ref."""
        n = itertools.count(1)

        def bind() -> str:
            return f":{next(n)}"

        values = [bind() for _ in range(5)]  # memory_id, session_id, agent_id, role, content
        values.append(f"VECTOR_EMBEDDING({self._onnx_model} USING {bind()} AS DATA)" if embed else "NULL")
        values.append("SYSTIMESTAMP")
        values += [bind(), bind(), bind()]  # target, metadata, content_txt
        values += [bind() if embed else "NULL", "SYSTIMESTAMP"]  # embedding_model, embedding_updated_at
        columns = self._INSERT_COLUMNS
        if row_columns:
            columns += self._INSERT_ROW_COLUMNS
            values += [bind() for _ in range(4)]
        return (
            f"INSERT /*+ NO_PARALLEL */ INTO {self._table} ({columns}) "  # nosec B608
            f"VALUES ({', '.join(values)})"
        )

    def _insert_binds(self, row: dict[str, Any], *, embed_chars: int | None, row_columns: bool) -> tuple[Any, ...]:
        """The bind tuple for _insert_sql. *embed_chars* is how much of the
        content goes to the embedding model; None is the NULL-embedding shape."""
        content = row.get("content")
        binds: list[Any] = [
            row.get("memory_id"),
            row.get("session_id") or self._session_id or "none",
            self._agent_id,
            row.get("role"),
            content,
        ]
        if embed_chars is not None:
            binds.append(self._safe_embed_text(content, embed_chars))
        binds += [row.get("target"), json.dumps(row.get("metadata") or {}), self._safe_varchar_bytes(content, 4000)]
        if embed_chars is not None:
            binds.append(self._onnx_model)
        if row_columns:
            binds += [row.get("turn_index"), row.get("chunk_index"), row.get("turn_id"), row.get("conv_ref")]
        return tuple(binds)

    def _insert_rows(
        self, rows: list[dict[str, Any]], run: Any, *, what: str, row_columns: bool, raise_duplicate: bool,
    ) -> list[tuple[str, str]]:
        """Write *rows* through *run(sql, binds_list)*, embedding in the database.

        Returns the (memory_id, content) pairs written with a NULL embedding,
        for the caller to hand to the backfill once they are committed.

        The ladder: a full-text embedding; on ORA-51815 (index snapshot too
        old) the same statement again; on ORA-54401 (input too long) a shorter
        text; on a transient server condition a NULL embedding, so the row is
        not lost. Anything else is raised. _TxAborted always propagates."""

        def binds(embed_chars: int | None) -> list[tuple[Any, ...]]:
            return [self._insert_binds(r, embed_chars=embed_chars, row_columns=row_columns) for r in rows]

        def insert_null() -> list[tuple[str, str]]:
            run(self._insert_sql(embed=False, row_columns=row_columns), binds(None))
            return [(str(r["memory_id"]), str(r["content"])) for r in rows if r.get("memory_id") and r.get("content")]

        if not self._onnx_model:
            raise _PolicyError(
                "In-database ONNX embedding model is unavailable; external fallback is disabled"
            )
        if self._onnx_in_cooldown():
            # Keep the rows (text search finds them at once) and let the
            # backfill embed them when the cooldown ends.
            logger.info("ONNX in cooldown; writing %d row(s) with NULL embeddings and queueing backfill", len(rows))
            return insert_null()

        sql = self._insert_sql(embed=True, row_columns=row_columns)
        try:
            run(sql, binds(_EMBED_TEXT_CHARS))
            return []
        except _TxAborted:
            raise
        except Exception as e:
            err_str = str(e)
            if raise_duplicate and "ORA-00001" in err_str:
                # Duplicate memory_id: not a model problem, and the
                # NULL-embedding fallback would hit the same constraint.
                raise
            is_51815 = "ORA-51815" in err_str
            is_54401 = "ORA-54401" in err_str
            is_shape_err = is_51815 or is_54401

            # 1. ORA-51815: retry transient snapshot conflicts
            if is_51815:
                for attempt in range(_EMBED_MAX_RETRIES):
                    try:
                        time.sleep(0.05 * (attempt + 1))
                        run(sql, binds(_EMBED_TEXT_CHARS))
                        return []
                    except _TxAborted:
                        raise
                    except Exception as retry_e:
                        err_str = str(retry_e)
                        if "ORA-51815" not in err_str:
                            is_54401 = "ORA-54401" in err_str
                            is_shape_err = is_54401
                            break

            # 2. ORA-54401: retry with conservative token clamp
            if is_54401:
                try:
                    run(sql, binds(_CONSERVATIVE_EMBED_CHARS))
                    return []
                except _TxAborted:
                    raise
                except Exception as retry_e:
                    err_str = str(retry_e)
                    is_51815 = "ORA-51815" in err_str
                    is_54401 = "ORA-54401" in err_str
                    is_shape_err = is_51815 or is_54401

            if not is_shape_err and not self._is_onnx_transient(err_str):
                self._onnx_mark_failed(err_str)
                logger.warning("In-database ONNX %s failed (%s); external fallback is disabled", what, err_str)
                raise RuntimeError(
                    f"In-database ONNX {what} failed ({err_str}); external fallback is disabled"
                ) from e

            # 3. Fallback to NULL embedding and background backfill (also for
            #    transient server conditions: the rows must not be lost).
            logger.warning(
                "In-database ONNX %s hit transient/shape error (%s); inserting with null embedding to prevent turn loss",
                what, err_str,
            )
            try:
                return insert_null()
            except _TxAborted:
                raise
            except Exception as null_e:
                logger.error("Null-embedding %s fallback failed (%s)", what, null_e)
                raise RuntimeError(
                    f"In-database ONNX {what} failed ({err_str}) and fallback {what} failed ({null_e})"
                ) from null_e

    def _insert(
        self,
        *,
        memory_id: str,
        session_id: str,
        role: str,
        content: str,
        target: str,
        metadata: dict | None = None,
    ) -> None:
        """Insert one row and commit. A duplicate memory_id raises the
        database error (ORA-00001) unchanged."""
        if not self._pool:
            raise RuntimeError(self._init_error or "pool not initialized")

        def run(sql: str, binds_list: list[tuple[Any, ...]]) -> None:
            pool = self._pool
            if pool is None:
                raise RuntimeError(self._init_error or "pool not initialized")
            with pool.acquire() as conn, conn.cursor() as cur:
                cur.execute(sql, binds_list[0])
                conn.commit()

        row = {
            "memory_id": memory_id, "session_id": session_id, "role": role,
            "content": content, "target": target, "metadata": metadata,
        }
        pending = self._insert_rows([row], run, what="insert", row_columns=False, raise_duplicate=True)
        for mid, text in pending:
            self._queue_embed_backfill(mid, text)

    def _update_row(self, mid: str, content: str | None, tags: list[str] | None = None) -> int:
        """Update a row's content (re-embedded in-database) and/or replace its
        metadata.tags. Returns the number of rows updated (0 = not found).
        Skill-corpus rows (role='skill') are never updated here: the next skill
        sync would silently revert the edit."""
        if not self._pool:
            raise RuntimeError(self._init_error or "pool not initialized")
        meta_set = ""
        meta_binds: dict[str, Any] = {}
        if tags is not None:
            meta_set = ", metadata = JSON_TRANSFORM(COALESCE(metadata, JSON('{}')), SET '$.tags' = JSON(:tags))"
            meta_binds = {"tags": json.dumps(list(tags))}
        if content is None:
            if tags is None:
                return 0
            sql = f"""
                UPDATE /*+ NO_PARALLEL */ {self._table}
                SET metadata = JSON_TRANSFORM(COALESCE(metadata, JSON('{{}}')), SET '$.tags' = JSON(:tags))
                WHERE memory_id = :id AND NVL(role, 'fact') <> 'skill'
            """  # nosec B608
            with self._acquire_checked() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, {"id": mid, **meta_binds})
                    n = cur.rowcount
                conn.commit()
            return n
        if not self._onnx_model:
            raise _PolicyError(
                "In-database ONNX model is not configured; external fallback is disabled"
            )
        txt = self._safe_varchar_bytes(content, 4000)
        embed_txt = self._safe_embed_text(content)
        if self._onnx_in_cooldown():
            # Same fallback as an ONNX transient error: store the new content
            # now (text search sees it at once) and re-embed it in the backfill.
            null_upd_sql = f"""
                UPDATE /*+ NO_PARALLEL */ {self._table}
                SET content = :c, embedding = NULL, content_txt = :t,
                    embedding_model = NULL, embedding_updated_at = SYSTIMESTAMP{meta_set}
                WHERE memory_id = :id AND NVL(role, 'fact') <> 'skill'
            """  # nosec B608
            with self._acquire_checked() as conn:
                with conn.cursor() as cur:
                    cur.execute(null_upd_sql, {"c": content, "t": txt, "id": mid, **meta_binds})
                    n = cur.rowcount
                conn.commit()
            if n:
                self._queue_embed_backfill(mid, content)
            return n
        backfill_after_commit = False
        with self._acquire_checked() as conn:
            with conn.cursor() as cur:
                succeeded = False
                try:
                    sql = f"""
                        UPDATE /*+ NO_PARALLEL */ {self._table}
                        SET content = :c, embedding = VECTOR_EMBEDDING({self._onnx_model} USING :e AS DATA), content_txt = :t,
                            embedding_model = :m, embedding_updated_at = SYSTIMESTAMP{meta_set}
                        WHERE memory_id = :id AND NVL(role, 'fact') <> 'skill'
                        """  # nosec B608
                    cur.execute(
                        sql,
                        {"c": content, "e": embed_txt, "t": txt, "m": self._onnx_model, "id": mid, **meta_binds},
                    )
                    succeeded = True
                except Exception as e:
                    err_str = str(e)
                    is_51815 = "ORA-51815" in err_str
                    is_54401 = "ORA-54401" in err_str
                    is_shape_err = is_51815 or is_54401

                    # 1. ORA-51815: retry transient snapshot conflicts
                    if is_51815:
                        for attempt in range(_EMBED_MAX_RETRIES):
                            try:
                                time.sleep(0.05 * (attempt + 1))
                                cur.execute(sql, {"c": content, "e": embed_txt, "t": txt, "m": self._onnx_model, "id": mid, **meta_binds})
                                succeeded = True
                                break
                            except Exception as retry_e:
                                err_str = str(retry_e)
                                if "ORA-51815" not in err_str:
                                    is_54401 = "ORA-54401" in err_str
                                    is_shape_err = is_54401
                                    break

                    # 2. ORA-54401: retry with conservative token clamp
                    if not succeeded and is_54401:
                        conservative_txt = self._safe_embed_text(content, _CONSERVATIVE_EMBED_CHARS)
                        try:
                            cur.execute(sql, {"c": content, "e": conservative_txt, "t": txt, "m": self._onnx_model, "id": mid, **meta_binds})
                            succeeded = True
                        except Exception as retry_e:
                            err_str = str(retry_e)
                            is_51815 = "ORA-51815" in err_str
                            is_54401 = "ORA-54401" in err_str
                            is_shape_err = is_51815 or is_54401

                    if not succeeded:
                        if not is_shape_err and not self._is_onnx_transient(err_str):
                            self._onnx_mark_failed(err_str)
                            logger.warning(
                                "In-database ONNX update failed (%s); external fallback is disabled",
                                err_str,
                            )
                            raise RuntimeError(
                                f"In-database ONNX update failed ({err_str}); external fallback is disabled"
                            ) from e

                        # 3. Fallback to NULL embedding and queue background backfill
                        #    (also for transient server conditions — keep
                        #    consistent with the _insert/_insert_many ladders:
                        #    no 60 s global cooldown + lost write for a
                        #    one-off ORA-01013/04031-class glitch)
                        logger.warning(
                            "In-database ONNX update hit transient/shape error (%s); updating row with null embedding and queueing backfill",
                            err_str,
                        )
                        null_upd_sql = f"""
                            UPDATE /*+ NO_PARALLEL */ {self._table}
                            SET content = :c, embedding = NULL, content_txt = :t,
                                embedding_model = NULL, embedding_updated_at = SYSTIMESTAMP{meta_set}
                            WHERE memory_id = :id AND NVL(role, 'fact') <> 'skill'
                        """  # nosec B608
                        try:
                            cur.execute(null_upd_sql, {"c": content, "t": txt, "id": mid, **meta_binds})
                            backfill_after_commit = True
                        except Exception as null_e:
                            raise RuntimeError(
                                f"In-database ONNX update failed ({err_str}) and fallback update failed ({null_e})"
                            ) from null_e
                n = cur.rowcount
            conn.commit()
        if backfill_after_commit and n:
            self._queue_embed_backfill(mid, content)
        return n

    def _is_skill_row(self, mid: str) -> bool:
        try:
            with self._acquire_checked() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT COUNT(*) FROM {self._table} WHERE memory_id = :id AND role = 'skill'",  # nosec B608
                        {"id": mid},
                    )
                    row = cur.fetchone()
            return bool(row and row[0])
        except Exception:
            return False

    def _upsert_fact(
        self,
        mid: str,
        content: str,
        *,
        target: str = "memory",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """oracle_add with a caller-chosen memory_id: update the row when it exists
        (content re-embedded; tags replaced only when given), otherwise insert it."""
        n = self._update_row(mid, content, tags=tags)
        if n:
            out: dict[str, Any] = {"result": "Fact updated (upsert).", "memory_id": mid, "upserted": True}
        else:
            try:
                self._insert(
                    memory_id=mid,
                    session_id=self._session_id,
                    role="fact",
                    content=content,
                    target=target,
                    metadata=metadata or {"kind": "tool"},
                )
                out = {"result": "Fact stored.", "memory_id": mid, "upserted": False}
            except Exception as e:
                if "ORA-00001" not in str(e):
                    raise
                # Lost a race with a concurrent writer of the same id: update instead.
                if not self._update_row(mid, content, tags=tags):
                    raise RuntimeError(
                        f"upsert of {mid} raced with a concurrent delete; nothing was written, retry"
                    ) from e
                out = {"result": "Fact updated (upsert).", "memory_id": mid, "upserted": True}
        if tags:
            out["tags"] = tags
        return out

    def _get_rows(
        self,
        *,
        memory_ids: list[str] | None = None,
        tags: list[str] | None = None,
        tag_match: str = "all",
        session_id: Any = None,
        agent_id: Any = None,
        target: str | None = None,
        limit: int = _GET_DEFAULT_LIMIT,
    ) -> list[dict[str, Any]]:
        """Exact lookup by memory_id(s) and/or tags (no embedding, no ONNX): the
        read side of a handoff. Newest first."""
        if not self._pool:
            raise RuntimeError(self._init_error or "pool not initialized")
        where = ""
        binds: dict[str, Any] = {}
        if memory_ids and len(memory_ids) > _GET_MAX_LIMIT:
            raise ValueError(f"oracle_get takes at most {_GET_MAX_LIMIT} memory_ids per call")
        if memory_ids:
            names = []
            for i, m in enumerate(memory_ids):
                binds[f"m{i}"] = m
                names.append(f":m{i}")
            where += f" AND memory_id IN ({', '.join(names)}) "
        if session_id:
            where += " AND session_id = :sid "
            binds["sid"] = session_id
        if agent_id:
            where += " AND agent_id = :aid "
            binds["aid"] = agent_id
        if target in ("memory", "user"):
            where += " AND target = :tgt "
            binds["tgt"] = target
        tag_sql, tag_binds = _tag_filter_sql(tags, tag_match)
        if not memory_ids and not tag_sql and not session_id:
            raise ValueError("oracle_get needs memory_id, memory_ids, tags, or session_id")
        where += tag_sql
        binds.update(tag_binds)
        binds["lim"] = max(1, min(int(limit or _GET_DEFAULT_LIMIT), _GET_MAX_LIMIT))
        sql = f"""
            SELECT /*+ NO_PARALLEL */ memory_id, session_id, agent_id, role, target, content,
                   metadata, created_at, embedding_updated_at
            FROM {self._table}
            WHERE 1 = 1 {where}
            ORDER BY COALESCE(embedding_updated_at, created_at) DESC, memory_id
            FETCH FIRST :lim ROWS ONLY
        """  # nosec B608 - table name validated at init; all values are bound
        out: list[dict[str, Any]] = []
        with self._acquire_checked() as conn:
            conn.call_timeout = self._search_ms
            with conn.cursor() as cur:
                cur.arraysize = max(_FETCH_ARRAYSIZE, binds["lim"] + 1)
                cur.prefetchrows = cur.arraysize
                _execute_lobs_as_str(cur, sql, binds)
                for row in cur.fetchall():
                    meta = _metadata_dict(row[6])
                    out.append(
                        {
                            "memory_id": row[0],
                            "session_id": row[1],
                            "agent_id": row[2],
                            "role": row[3],
                            "target": row[4],
                            "content": _lob_text(row[5]),
                            "tags": list(meta.get("tags") or []),
                            "kind": meta.get("kind"),
                            "created_at": row[7],
                            "updated_at": row[8] or row[7],
                        }
                    )
        return out

    def _insert_many(
        self,
        rows: list[dict[str, Any]],
        tx_conn: Any = None,
    ) -> list[tuple[str, str]]:
        """Insert *rows*. Without *tx_conn* each attempt runs on its own pooled
        connection and commits; returns [].

        With *tx_conn* every statement runs on the caller's connection inside the
        caller's transaction and nothing is committed: a failed attempt is rolled
        back to a savepoint (a partial array insert must not leave rows behind
        for the retry). The caller commits, then queues the returned
        (memory_id, content) pairs with _queue_embed_backfill — queuing before the
        commit would let the backfill look for rows nobody else can see yet."""
        if tx_conn is None and not self._pool:
            raise RuntimeError(self._init_error or "pool not initialized")

        if not rows:
            return []

        pending = self._insert_rows(
            rows, lambda sql, binds_list: self._exec_insert(sql, binds_list, tx_conn),
            what="insert_many", row_columns=True, raise_duplicate=False,
        )
        if tx_conn is not None:
            return pending  # caller queues after its commit
        for mid, text in pending:
            self._queue_embed_backfill(mid, text)
        return []

    def _exec_insert(self, sql: str, binds_list: list[Any], tx_conn: Any = None) -> None:
        """executemany on a fresh pooled connection (and commit), or inside the
        caller's open transaction on *tx_conn* (no commit; a failure is rolled
        back to a savepoint so earlier statements in that transaction survive).

        With *tx_conn*, a session-level error or a failed savepoint rollback
        raises _TxAborted: the connection cannot run another attempt, so the
        retry/clamp/NULL-fallback ladder in _insert_many must stop and let the
        caller roll back the whole transaction."""
        if tx_conn is None:
            pool = self._pool
            if pool is None:
                raise RuntimeError(self._init_error or "pool not initialized")
            with pool.acquire() as conn, conn.cursor() as cur:
                cur.executemany(sql, binds_list)
                conn.commit()
            return
        with tx_conn.cursor() as cur:
            cur.execute("SAVEPOINT hermes_insert_many")
            try:
                cur.executemany(sql, binds_list)
            except Exception as e:
                if _is_session_error(e):
                    raise _TxAborted(f"transaction connection broken: {e}") from e
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT hermes_insert_many")
                except Exception as rb_e:
                    raise _TxAborted(f"{e} (rollback to savepoint failed: {rb_e})") from e
                raise

    _JUNK = (
        " AND memory_id NOT LIKE 'test_%'"
        " AND NVL(session_id, 'x') <> 'diagnostic-session-001'"
        " AND NVL(role, 'fact') NOT IN ('turn', 'assistant', 'session') "
        # lifecycle telemetry only ([skill_usage], [skill_vectorization_*]).
        # MUST NOT match skill_ingest rows — those are the skill index.
        + _telemetry_sql()
    )

    @staticmethod
    def _is_prefix_match(content: str | None, query: str | None) -> bool:
        if not content or not query:
            return False
        c_clean = str(content).strip().lower()
        q_clean = str(query).strip().lower()
        c_stripped = re.sub(r"^(\s*\d+[\.\)]\s*|[\s#*\->\[\]\"'(`:]+)+", "", c_clean)
        q_stripped = re.sub(r"^(\s*\d+[\.\)]\s*|[\s#*\->\[\]\"'(`:]+)+", "", q_clean)
        if not q_stripped or not c_stripped:
            return False
        if c_stripped.startswith(q_stripped) or c_clean.startswith(q_clean):
            first_w = q_stripped.split()[0] if q_stripped.split() else ""
            if len(q_stripped.split()) == 1 and first_w in _PREFIX_STOP_WORDS:
                return False
            rem = c_stripped[len(q_stripped):]
            if not rem or re.match(r"^[\s:;.,_\-—/\]\)#*]", rem):
                return True

        tokens = [t.strip(".,;:!?\"'()[]{}") for t in q_stripped.split()]
        title_tokens = [t for t in tokens if len(t) > 2 and t not in _PREFIX_STOP_WORDS]
        if not title_tokens:
            return False
        first_title = title_tokens[0]

        if c_stripped.startswith(first_title):
            rem = c_stripped[len(first_title):]
            if not rem or re.match(r"^[\s:;.,_\-—/\]\)#*]", rem):
                return True
        return False

    def _fetch_rows(self, cur, sql: str, binds: dict, max_rows: int | None = None) -> list:
        """Run one search query and map its rows. *max_rows* is the most rows the
        statement can return (2 x k for the fused UNION ALL query); the fetch
        array covers it, plus one, so the whole result arrives with the execute
        round trip instead of needing another fetch."""
        if max_rows is None:
            max_rows = int(binds.get("k") or 0)
        cur.arraysize = max(_FETCH_ARRAYSIZE, int(max_rows) + 1)
        cur.prefetchrows = cur.arraysize
        _execute_lobs_as_str(cur, sql, binds)
        out = []
        for row in cur.fetchall():
            content = row[4]
            if content is not None and not isinstance(content, str):
                # Older drivers without fetch_lobs=False return a LOB locator;
                # read it out fully (one extra round trip per row).
                content = content.read() if hasattr(content, "read") else str(content)
            dist = float(row[7]) if row[7] is not None else None
            txt = float(row[6] or 0)
            vec_score = max(0.0, min(1.0, 1.0 - dist)) if dist is not None else 0.0
            score = (
                0.7 * vec_score + 0.3 * min(1.0, txt / 100.0)
                if dist is not None
                else min(1.0, txt / 100.0)
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
                    "dist": dist,
                    "created_at": row[8] if len(row) > 8 else None,
                }
            )
            tags = _metadata_tags(row[9]) if len(row) > 9 else []
            if tags:
                out[-1]["tags"] = tags
        return out

    def _fetch_rows_attempt(self, cur: Any, sql: str, binds: dict, attempt: int, max_rows: int | None = None) -> list:
        """_fetch_rows for one attempt of an ORA-51815 retry loop: the first
        attempt uses *cur*, every re-execution of the same SQL a fresh cursor on
        the same connection, so a statement left in a bad state by the failed
        attempt is never re-used."""
        conn = getattr(cur, "connection", None) if attempt else None
        if conn is None:
            return self._fetch_rows(cur, sql, binds, max_rows=max_rows)
        with conn.cursor() as fresh:
            return self._fetch_rows(fresh, sql, binds, max_rows=max_rows)

    _SEARCH_COLUMNS = "memory_id, session_id, agent_id, role, content, target"
    _TAGS_COLUMN = "JSON_QUERY(metadata, '$.tags' RETURNING VARCHAR2(4000) NULL ON ERROR) AS tags"

    def _row_filters(
        self, tgt: str = "any", *, session_id: Any = None, agent_id: Any = None,
        tags: Any = None, tag_match: str = "all",
    ) -> tuple[str, dict]:
        """The predicates every search leg shares, as SQL (each starting with
        " AND") and its binds: junk rows out, then the target, session, agent
        and tag filters the caller asked for."""
        sql = self._JUNK
        binds: dict[str, Any] = {}
        if tgt not in ("", "any"):
            sql += " AND target = :tgt "
            binds["tgt"] = tgt
        if session_id:
            sql += " AND session_id = :sid "
            binds["sid"] = session_id
        if agent_id:
            sql += " AND agent_id = :aid "
            binds["aid"] = agent_id
        tag_sql, tag_binds = _tag_filter_sql(tags, tag_match)
        sql += tag_sql
        binds.update(tag_binds)
        return sql, binds

    def _text_leg_sql(self, filters: str) -> str:
        """Oracle Text candidates, best first. Binds :q (CONTAINS query), :k."""
        return f"""
            SELECT /*+ NO_PARALLEL */ {self._SEARCH_COLUMNS},
                   SCORE(1) AS txt_score, NULL AS distance, created_at,
                   {self._TAGS_COLUMN}
            FROM {self._table}
            WHERE CONTAINS(content, :q, 1) > 0
            {filters}
            ORDER BY txt_score DESC, created_at DESC
            FETCH FIRST :k ROWS ONLY
        """  # nosec B608 - table and model names are sanitized at init; values are bound

    def _vector_leg_sql(self, filters: str) -> str:
        """Nearest rows to the query, embedded in the database. Binds :qv, :k."""
        return f"""
            SELECT /*+ NO_PARALLEL */ {self._SEARCH_COLUMNS},
                   0 AS txt_score,
                   VECTOR_DISTANCE(embedding, VECTOR_EMBEDDING({self._onnx_model} USING :qv AS DATA), COSINE) AS distance,
                   created_at, {self._TAGS_COLUMN}
            FROM {self._table}
            WHERE embedding IS NOT NULL
            {filters}
            ORDER BY distance ASC, created_at DESC
            FETCH FIRST :k ROWS ONLY
        """  # nosec B608

    def _build_text_search_sql(self, contains_q: str, fetch_k: int, tgt: str = "any", **filters: Any) -> tuple[str, dict]:
        where, binds = self._row_filters(tgt, **filters)
        binds.update({"q": contains_q, "k": fetch_k})
        return self._text_leg_sql(where), binds

    def _build_vector_search_sql(self, query: str, fetch_k: int, tgt: str = "any", **filters: Any) -> tuple[str, dict]:
        where, binds = self._row_filters(tgt, **filters)
        binds.update({"qv": self._safe_embed_text(query), "k": fetch_k})
        return self._vector_leg_sql(where), binds

    def _build_hybrid_search_sql(
        self, query: str, contains_q: str, fetch_k: int, tgt: str = "any", **filters: Any
    ) -> tuple[str, dict]:
        """Both legs in one statement: up to :k vector rows and up to :k text rows."""
        where, binds = self._row_filters(tgt, **filters)
        binds.update({"qv": self._safe_embed_text(query), "q": contains_q, "k": fetch_k})
        columns = f"{self._SEARCH_COLUMNS}, txt_score, distance, created_at, tags"
        sql = f"""
            SELECT /*+ NO_PARALLEL */ {columns}
            FROM ({self._vector_leg_sql(where)})
            UNION ALL
            SELECT /*+ NO_PARALLEL */ {columns}
            FROM ({self._text_leg_sql(where)})
        """  # nosec B608
        return sql, binds

    def _execute_hybrid_search(
        self, cur: Any, query: str, contains_q: str, fetch_k: int, tgt: str = "any", *,
        session_id: Any = None, agent_id: Any = None, timeout_s: float | None = None,
        tags: Any = None, tag_match: str = "all",
    ) -> list:
        if timeout_s is None:
            timeout_s = self._search_secs
        merged: dict = {}
        if self._onnx_in_cooldown():
            logger.info("ONNX model is in cooldown; executing text search for hybrid query")
            try:
                sql, binds = self._build_text_search_sql(
                    contains_q, fetch_k, tgt, session_id=session_id, agent_id=agent_id, tags=tags, tag_match=tag_match
                )
                for r in self._fetch_rows(cur, sql, binds):
                    r.pop("dist", None)
                    # Same weight as a text-only hit of the fused query, so a
                    # hybrid score means the same thing on every path.
                    r["score"] = round(r["score"] * _HYBRID_TEXT_WEIGHT, 4)
                    merged[r["memory_id"]] = r
                for item in merged.values():
                    if self._is_prefix_match(item.get("content"), query):
                        item["score"] = round(min(1.0, item["score"] + 0.15), 4)
            except Exception as e:
                err_str = str(e)
                is_timeout_err = _is_timeout_error(e)
                if is_timeout_err:
                    logger.warning(
                        "Oracle hybrid text search in cooldown timed out after %ss (%s); failing fast",
                        timeout_s,
                        e,
                    )
                    raise TimeoutError(
                        f"oracle_search hybrid text query timed out after {timeout_s:.0f}s: {e}"
                    ) from e
                raise
            return sorted(merged.values(), key=lambda x: x["score"], reverse=True)

        if not self._onnx_model:
            raise RuntimeError(
                "In-database ONNX model is not configured; external fallback is disabled"
            )
        fused_sql, fused_binds = self._build_hybrid_search_sql(
            query, contains_q, fetch_k, tgt, session_id=session_id, agent_id=agent_id, tags=tags, tag_match=tag_match
        )

        degraded_to_text = False
        for attempt in range(_51815_RETRIES):
            try:
                for r in self._fetch_rows_attempt(cur, fused_sql, fused_binds, attempt, max_rows=2 * fetch_k):
                    prev = merged.get(r["memory_id"])
                    if prev:
                        prev["score"] = round(
                            min(1.0, prev["score"] + r["score"] * 0.3), 4
                        )
                        prev.pop("dist", None)
                    else:
                        item = dict(r)
                        if item.get("dist") is None and "dist" in item:
                            item["score"] = round(item["score"] * _HYBRID_TEXT_WEIGHT, 4)
                        item.pop("dist", None)
                        merged[r["memory_id"]] = item
                break
            except Exception as e:
                err_str = str(e)
                if _is_51815(e):
                    if attempt < _51815_RETRIES - 1:
                        time.sleep(_51815_BACKOFF_SECS * (attempt + 1))
                        continue
                    logger.warning(
                        "In-database ONNX hybrid search exhausted ORA-51815 retries (%d attempts); degrading to text search",
                        _51815_RETRIES,
                    )
                    degraded_to_text = True
                    break

                is_text_err = any(code in err_str for code in ("DRG-", "ORA-29902", "ORA-30600"))
                is_shape_err = any(code in err_str for code in ("ORA-54401", "ORA-51815"))
                is_timeout_err = _is_timeout_error(e)
                if not is_text_err and not is_shape_err and not is_timeout_err:
                    self._onnx_mark_failed(err_str)

                if is_timeout_err:
                    logger.warning(
                        "Oracle search query timed out after %ss (%s); failing fast",
                        timeout_s,
                        e,
                    )
                    raise TimeoutError(
                        f"oracle_search query timed out after {timeout_s:.0f}s: {e}"
                    ) from e

                if is_text_err:
                    logger.warning(
                        "Oracle Text error in hybrid search (%s); falling back to pure vector search",
                        e,
                    )
                    vec_fallback_done = False
                    for vec_attempt in range(_51815_RETRIES):
                        try:
                            vec_sql, vec_binds = self._build_vector_search_sql(
                                query, fetch_k, tgt, session_id=session_id, agent_id=agent_id, tags=tags, tag_match=tag_match
                            )
                            for r in self._fetch_rows_attempt(cur, vec_sql, vec_binds, vec_attempt):
                                r.pop("dist", None)
                                merged[r["memory_id"]] = r
                            vec_fallback_done = True
                            break
                        except Exception as vec_err:
                            if _is_51815(vec_err):
                                if vec_attempt < _51815_RETRIES - 1:
                                    time.sleep(_51815_BACKOFF_SECS * (vec_attempt + 1))
                                    continue
                                logger.warning(
                                    "In-database ONNX vector fallback exhausted ORA-51815 retries (%d attempts); degrading to text search",
                                    _51815_RETRIES,
                                )
                                degraded_to_text = True
                                break

                            vec_err_str = str(vec_err)
                            is_vec_timeout = _is_timeout_error(vec_err)
                            logger.warning(
                                "In-database ONNX vector fallback failed (%s); external fallback is disabled",
                                vec_err,
                            )
                            if not is_vec_timeout and not any(code in vec_err_str for code in ("ORA-54401", "ORA-51815")):
                                self._onnx_mark_failed(vec_err)
                            if is_vec_timeout:
                                raise TimeoutError(
                                    f"oracle_search vector fallback query timed out after {timeout_s:.0f}s: {vec_err}"
                                ) from vec_err
                            raise RuntimeError(
                                f"In-database ONNX vector fallback failed ({vec_err}); external fallback is disabled"
                            ) from vec_err
                    if vec_fallback_done or degraded_to_text:
                        break
                else:
                    logger.warning(
                        "In-database ONNX hybrid search failed (%s); external fallback is disabled",
                        e,
                        # Non-Oracle errors (e.g. "'NoneType' object is not iterable") are
                        # driver/plugin bugs: keep the traceback so the next one is diagnosable.
                        exc_info=not _ORA_CODE_RE.search(err_str),
                    )
                    raise RuntimeError(
                        f"In-database ONNX hybrid search failed ({e}); external fallback is disabled"
                    ) from e

        if degraded_to_text:
            merged.clear()
            try:
                sql, binds = self._build_text_search_sql(
                    contains_q, fetch_k, tgt, session_id=session_id, agent_id=agent_id, tags=tags, tag_match=tag_match
                )
                for r in self._fetch_rows(cur, sql, binds):
                    r.pop("dist", None)
                    # Same weight as a text-only hit of the fused query, so a
                    # hybrid score means the same thing on every path.
                    r["score"] = round(r["score"] * _HYBRID_TEXT_WEIGHT, 4)
                    merged[r["memory_id"]] = r
                for item in merged.values():
                    if self._is_prefix_match(item.get("content"), query):
                        item["score"] = round(min(1.0, item["score"] + 0.15), 4)
            except Exception as e:
                err_str = str(e)
                is_timeout_err = _is_timeout_error(e)
                if is_timeout_err:
                    logger.warning(
                        "Oracle hybrid degraded text search timed out after %ss (%s); failing fast",
                        timeout_s,
                        e,
                    )
                    raise TimeoutError(
                        f"oracle_search hybrid degraded text query timed out after {timeout_s:.0f}s: {e}"
                    ) from e
                logger.warning("Oracle hybrid degraded text search encountered error: %s", e)
            return sorted(merged.values(), key=lambda x: x["score"], reverse=True)

        for item in merged.values():
            item.pop("dist", None)
            if self._is_prefix_match(item.get("content"), query):
                item["score"] = round(min(1.0, item["score"] + 0.15), 4)

        # Not cut to top_k here: weighting and per-source diversification run
        # in _search_once over the whole candidate list, and the optional rerank
        # after them.
        return sorted(merged.values(), key=lambda x: x["score"], reverse=True)

    def _apply_weights(self, rows: list[dict[str, Any]], cur: Any = None) -> list[dict[str, Any]]:
        """Apply retrieval weighting to candidate rows based on recency, role, telemetry, and length."""
        if not rows:
            return rows

        if not isinstance(rows, list):
            rows = list(rows)

        try:
            now = datetime.now(timezone.utc)
            recency_floor = self._tune("recency_floor")
            recency_scale_days = self._tune("recency_scale_days")
            telemetry_weight = self._tune("telemetry_weight")
            for r in rows:
                if not isinstance(r, dict):
                    continue
                try:
                    raw_score = r.get("score")
                    base_score = float(raw_score) if raw_score is not None else 0.0
                except (ValueError, TypeError):
                    base_score = 0.0
                role = str(r.get("role") or "fact").strip().lower()
                content = str(r.get("content") or "")
                stripped_content = content.strip()

                # 1. Recency multiplier: 0.85 + 0.15 * e^(-age_days / 120) -> range [0.85, 1.00]
                recency_mult = 1.0
                dt = r.get("created_at")

                if dt is not None:
                    if isinstance(dt, str):
                        try:
                            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
                        except Exception:
                            dt = None
                    elif isinstance(dt, (int, float)):
                        try:
                            dt = datetime.fromtimestamp(dt, tz=timezone.utc)
                        except Exception:
                            dt = None
                    elif isinstance(dt, date) and not isinstance(dt, datetime):
                        dt = datetime.combine(dt, datetime.min.time(), tzinfo=timezone.utc)

                    if isinstance(dt, datetime):
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        age_seconds = max(0.0, (now - dt).total_seconds())
                        age_days = age_seconds / 86400.0
                        recency_mult = recency_floor + (1.0 - recency_floor) * math.exp(
                            -age_days / recency_scale_days
                        )

                # 2. Role multiplier
                role_mult = _WEIGHT_ROLE.get(role, 1.0)

                # 3. Telemetry multiplier
                telemetry_mult = 1.0
                if any(stripped_content.startswith(p) or stripped_content.lower().startswith(p.lower()) for p in _TELEMETRY_PREFIXES):
                    telemetry_mult = telemetry_weight

                # 4. Short content multiplier (< 25 chars)
                short_mult = 1.0
                if len(stripped_content) < _WEIGHT_SHORT_CHARS:
                    short_mult = _WEIGHT_SHORT_FACTOR

                r["score"] = round(base_score * recency_mult * role_mult * telemetry_mult * short_mult, 4)

            rows.sort(
                key=lambda x: (x.get("score") or 0.0) if isinstance(x, dict) else 0.0,
                reverse=True,
            )
            return self._diversify_rows(rows)
        except Exception as e:
            logger.debug("weight calculation failed in _apply_weights: %s", e)
            return rows

    def _diversify_rows(self, rows: list, max_per_source: int | None = None) -> list:
        """Cap how many chunks a single source (conv_ref) may contribute.

        Chunked skills produce dozens of near-identical embeddings, so without a cap one
        skill can occupy every result slot purely on chunk multiplicity and crowd out
        unrelated facts. Rows with no conv_ref (facts, turns) are never capped.
        """
        limit = self._tune("max_chunks_per_source") if max_per_source is None else max_per_source
        if not limit or limit <= 0:
            return rows
        counts: dict[str, int] = {}
        kept: list = []
        for row in rows:
            key = _source_key(row)
            if not key:
                kept.append(row)
                continue
            if counts.get(key, 0) >= limit:
                continue
            counts[key] = counts.get(key, 0) + 1
            kept.append(row)
        return kept

    def _search(self, query: str, *args: Any, **kwargs: Any) -> list:
        """Search; on a broken-session error (ORA-01002, lost connection, or a
        driver-level non-Oracle exception) retry ONCE on a fresh connection.
        The bad connection is dropped from the pool by _acquire_checked, so the
        retry cannot be handed the same poisoned session. Timeouts are not
        retried (they already consumed the caller's budget)."""
        try:
            return self._search_once(query, *args, **kwargs)
        except Exception as e:
            if _is_timeout_error(e):
                raise
            if not (_is_session_error(e) or not _ORA_CODE_RE.search(str(e))):
                raise
            logger.warning("oracle search hit a session-level error (%s); retrying once on a fresh connection", e)
            return self._search_once(query, *args, **kwargs)

    def _acquire_checked(self):
        """Pool acquire that DROPS (rather than returns) a connection whose use
        raised a session-level or non-Oracle error, so it is never reused."""
        import contextlib

        @contextlib.contextmanager
        def _cm():
            pool = self._pool
            if pool is None:
                raise RuntimeError(self._init_error or "pool not initialized")
            conn = pool.acquire()
            try:
                yield conn
            except BaseException as exc:
                # Session errors, call timeouts and non-Oracle exceptions all
                # drop the connection. After a call timeout (DPY-4024) the thin
                # driver's statement cache can keep a cancelled cursor id; reusing
                # that session produced ORA-01002 and then "'NoneType' object is
                # not iterable" on later searches. A reconnect is far cheaper.
                bad = _is_session_error(exc) or _is_timeout_error(exc)
                bad = bad or (
                    isinstance(exc, Exception)
                    and not isinstance(exc, _PolicyError)
                    and not _ORA_CODE_RE.search(str(exc))
                )
                if bad:
                    try:
                        pool.drop(conn)
                        conn = None
                        logger.info("oracle: dropped pooled connection after session-level error or timeout: %s", exc)
                    except Exception as drop_e:
                        logger.debug("oracle pool drop failed: %s", drop_e)
                raise
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception as close_e:
                        logger.debug("oracle conn release failed: %s", close_e)

        return _cm()

    def _search_once(
        self,
        query: str,
        top_k: int = 8,
        mode: str = "hybrid",
        target: str = "any",
        rerank: bool = True,
        session_id: Any = None,
        agent_id: Any = None,
        timeout: float | None = None,
        tags: Any = None,
        tag_match: str = "all",
    ) -> list:
        if not self._pool or not query:
            return []
        top_k = max(1, min(int(top_k or 8), 30))
        mode = (mode or "hybrid").lower()
        tgt = (target or "any").lower()
        contains_q = self._contains_query(query)
        if not self._onnx_model and mode != "text":
            raise RuntimeError(
                "In-database ONNX model is not configured; external fallback is disabled"
            )

        # Candidate generation must be generous: role weighting, the prefix boost and
        # per-source diversification all run AFTER this cut, so a narrow window lets
        # whichever corpus happens to be densest in embedding space monopolise results
        # (the v1.0.9.6 starvation bug was the extreme case of this).
        pool_size = self._tune("min_candidate_pool")
        fetch_k = max(top_k * 2, pool_size) if mode == "hybrid" else max(top_k, pool_size)
        query_timeout_s = float(timeout) if timeout is not None else self._search_secs
        query_timeout_ms = int(query_timeout_s * 1000)
        with self._acquire_checked() as conn:
            try:
                conn.call_timeout = query_timeout_ms
            except Exception:
                pass
            with conn.cursor() as cur:
                if mode == "text":
                    try:
                        sql, binds = self._build_text_search_sql(
                            contains_q, fetch_k, tgt, session_id=session_id, agent_id=agent_id, tags=tags, tag_match=tag_match
                        )
                        rows = self._fetch_rows(cur, sql, binds)
                        for r in rows:
                            r.pop("dist", None)
                            if self._is_prefix_match(r.get("content"), query):
                                r["score"] = round(min(1.0, r["score"] + 0.15), 4)
                        rows = sorted(rows, key=lambda x: x["score"], reverse=True)
                    except Exception as e:
                        err_str = str(e)
                        is_timeout_err = _is_timeout_error(e)
                        if is_timeout_err:
                            logger.warning(
                                "Oracle text search timed out after %ss (%s); failing fast",
                                query_timeout_s,
                                e,
                            )
                            raise TimeoutError(
                                f"oracle_search text query timed out after {query_timeout_s:.0f}s: {e}"
                            ) from e
                        raise
                elif mode == "vector":
                    rows = []
                    if self._onnx_in_cooldown():
                        raise RuntimeError(
                            "In-database ONNX embedding model is in cooldown; external fallback is disabled"
                        )
                    try:
                        sql, binds = self._build_vector_search_sql(
                            query, fetch_k, tgt, session_id=session_id, agent_id=agent_id, tags=tags, tag_match=tag_match
                        )
                        for attempt in range(_51815_RETRIES):
                            try:
                                rows = self._fetch_rows_attempt(cur, sql, binds, attempt)
                                break
                            except Exception as vec_fetch_e:
                                if _is_51815(vec_fetch_e) and attempt < _51815_RETRIES - 1:
                                    time.sleep(_51815_BACKOFF_SECS * (attempt + 1))
                                    continue
                                raise
                        for r in rows:
                            r.pop("dist", None)
                            if self._is_prefix_match(r.get("content"), query):
                                r["score"] = round(min(1.0, r["score"] + 0.15), 4)
                        rows = sorted(rows, key=lambda x: x["score"], reverse=True)
                    except Exception as e:
                        err_str = str(e)
                        is_shape_err = any(code in err_str for code in ("ORA-54401", "ORA-51815"))
                        is_timeout_err = _is_timeout_error(e)
                        logger.warning(
                            "In-database ONNX vector search failed (%s); external fallback is disabled",
                            e,
                        )
                        if not is_shape_err and not is_timeout_err:
                            self._onnx_mark_failed(e)
                        if is_timeout_err:
                            raise TimeoutError(
                                f"oracle_search vector query timed out after {query_timeout_s:.0f}s: {e}"
                            ) from e
                        raise RuntimeError(
                            f"In-database ONNX vector search failed ({e}); external fallback is disabled"
                        ) from e
                else:
                    rows = self._execute_hybrid_search(
                        cur, query, contains_q, fetch_k, tgt, session_id=session_id, agent_id=agent_id,
                        tags=tags, tag_match=tag_match, timeout_s=query_timeout_s,
                    )
                rows = self._apply_weights(rows, cur)
                if mode == "hybrid" and rerank and len(rows) > 1:
                    rows = self._rerank(query, rows, top_k, cur=cur)
        return rows[:top_k]

    def _rerank(self, query: str, rows: list, top_k: int, cur=None) -> list:
        """Best-effort DBMS_VECTOR.RERANK; identity if the call is unavailable."""
        if not rows:
            return rows
        if not self._tune("rerank_enabled"):
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
            # Reorder only; the caller cuts to top_k (rows the reranker did not
            # see keep their order after the reranked head).
            return sorted(rows, key=lambda r: id_rank.get(r["memory_id"], len(id_rank)))
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
            # The stored text is data: escaped, it cannot close the boundary
            # tag or open another one. Cut first, so no entity is cut in half.
            body = html.escape(body, quote=False)
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
            pf_thread = _spawn_thread(_run, name="oracle-prefetch")
            self._prefetch_thread = pf_thread
        # Start the thread created under the lock, never re-read the attribute:
        # a concurrent call may already have replaced it (double start()).
        pf_thread.start()

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
        """Mirror built-in memory writes. add inserts an md_ row; replace inserts
        the new entry first, then deletes the mirror of the previous one; remove
        deletes it. The previous entry is identified only by
        metadata["previous_content"] (old_text alone is not authoritative), and
        only this host's mirrors of the same target are touched."""
        if not self._pool or action not in ("add", "replace", "remove"):
            return
        meta = dict(metadata or {})
        previous = meta.pop("previous_content", None)
        meta.pop("old_text", None)
        tgt = target if target in ("memory", "user") else "memory"
        try:
            if action in ("add", "replace") and isinstance(content, str) and content:
                self._insert(
                    memory_id=f"md_{uuid.uuid4().hex[:16]}",
                    session_id=self._session_id,
                    role="fact",
                    content=content,
                    target=tgt,
                    metadata={**meta, "kind": "builtin", "host": get_host_id()},
                )
            if action == "add":
                return
            if not isinstance(previous, str) or not previous:
                logger.debug("oracle on_memory_write %s without previous_content; old mirror kept", action)
                return
            if action == "replace" and previous == content:
                return
            self._delete_memory_mirror(tgt, previous)
        except Exception as e:
            logger.debug("oracle on_memory_write %s failed: %s", action, e)

    def _delete_memory_mirror(self, target: str, content: str) -> int:
        """Delete this host's md_ mirror rows of one built-in memory entry. Rows
        mirrored before 1.1.1.4 carry no host and match by agent id alone. The
        host is read from the row itself (JSON_QUERY wrapper), not from the
        asynchronously synced JSON search index, so a mirror written a moment
        ago is found."""
        sql = f"""
            DELETE /*+ NO_PARALLEL */ FROM {self._table}
            WHERE memory_id LIKE 'md\\_%' ESCAPE '\\'
              AND role = 'fact' AND agent_id = :aid AND NVL(target, 'memory') = :tgt
              AND NVL(JSON_VALUE(JSON_QUERY(metadata, '$' RETURNING JSON), '$.host'), :host) = :host
              AND DBMS_LOB.COMPARE(content, TO_CLOB(:c)) = 0
        """  # nosec B608
        with self._acquire_checked() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"aid": self._agent_id, "tgt": target, "host": get_host_id(), "c": content})
                n = cur.rowcount or 0
            conn.commit()
        return n

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

    def _live_provider_for(self, session_id: str = "") -> "OracleMemoryProvider | None":
        """Self if usable, else a live provider in this process (prefer the one
        serving *session_id*). None when no provider has an open pool."""
        if self._pool is not None and not self._write_stop.is_set():
            return self
        with _LIVE_PROVIDERS_LOCK:
            live = [p for p in _LIVE_PROVIDERS
                    if p is not self and p._pool is not None and not p._write_stop.is_set()]
        if not live:
            return None
        if session_id:
            for p in live:
                if p._session_id == session_id:
                    return p
        return max(live, key=lambda p: getattr(p, "_live_seq", 0))

    def on_skill_lifecycle(self, *args: Any, **kwargs: Any) -> None:
        """Global hook entry point. Hermes binds this hook to whichever provider
        instance loaded LAST; once that instance's agent shuts down its pool is
        gone, so route to a live provider instead of failing every skill event
        in every other session with "pool not initialized"."""
        data = args[0] if args and isinstance(args[0], dict) else {}
        sid = kwargs.get("session_id") or data.get("session_id") or ""
        target = self._live_provider_for(sid if isinstance(sid, str) else "") or self
        # (falling back to self is safe: _queue_background_insert now drops
        # rather than enqueues when this instance has no usable pool)
        target._on_skill_lifecycle_impl(*args, **kwargs)

    def _on_skill_lifecycle_impl(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Observer hook recording skill usage/load and handling live lifecycle ingestion into Oracle 26ai ADB memory."""
        data = args[0] if args and isinstance(args[0], dict) else {}
        action = kwargs.get("action") or data.get("action")
        if not action and args and isinstance(args[0], str):
            action = args[0]
            if len(args) > 1 and isinstance(args[1], str) and "skill_name" not in kwargs:
                kwargs = dict(kwargs)
                kwargs["skill_name"] = args[1]

        skill_name = (
            kwargs.get("skill_name")
            or data.get("skill_name")
            or (args[1] if len(args) > 1 and isinstance(args[1], str) else "")
        )

        if action in ("used", "loaded"):
            try:
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
                logger.debug("oracle on_skill_lifecycle usage log failed: %s", exc)
            return

        # Content actions ingest the skill; "archived" retires this host's copy.
        # The caller (the skill tool) only schedules: file lookup, parsing, the
        # hash check and the write all run on the per-skill writer thread (#6).
        if action in ("created", "edited", "patched", "installed", "restored", "archived"):
            if not isinstance(skill_name, str) or not _SKILL_NAME_RE.match(skill_name):
                logger.debug("on_skill_lifecycle: ignoring invalid skill name %r", skill_name)
                return
            try:
                # Resolved here: the Hermes home can be a context-local
                # (per-profile) override that a new thread does not inherit.
                roots = list(_sync_skills_module().resolve_skill_roots())
            except Exception as e:
                logger.debug("on_skill_lifecycle: cannot resolve skill roots: %s", e)
                return
            self._schedule_skill_lifecycle(skill_name, "archive" if action == "archived" else "ingest", roots)
            return

    def _lifecycle_key(self, skill_name: str) -> tuple[str, str, str]:
        return (self._pool_fingerprint, self._table, skill_name)

    def _schedule_skill_lifecycle(self, skill_name: str, op: str, roots: list[Path]) -> None:
        """Run *op* for *skill_name* on a writer thread, one writer per skill
        and database table in this process (whichever provider instance the
        event reached). An event for a skill whose writer is still busy is
        coalesced, not dropped: the writer runs once more with the latest event."""
        key = self._lifecycle_key(skill_name)
        with _LIFECYCLE_LOCK:
            if key in _LIFECYCLE_IN_FLIGHT:
                _LIFECYCLE_PENDING[key] = (self, op, roots)
                return
            _LIFECYCLE_IN_FLIGHT.add(key)
        thread = _spawn_thread(
            self._run_skill_lifecycle, args=(key, skill_name, op, roots), name=f"oracle-lifecycle-{skill_name}"
        )
        started = False
        try:
            started = self._start_sync_thread(thread)
        except Exception as e:
            logger.debug("on_skill_lifecycle: cannot start writer for '%s': %s", skill_name, e)
        if not started:
            logger.info("on_skill_lifecycle: provider shutting down; not processing '%s'", skill_name)
            with _LIFECYCLE_LOCK:
                _LIFECYCLE_IN_FLIGHT.discard(key)
                _LIFECYCLE_PENDING.pop(key, None)

    def _run_skill_lifecycle(self, key: tuple[str, str, str], skill_name: str, op: str, roots: list[Path]) -> None:
        """Writer loop for one skill. *key* is the one computed when the work
        was scheduled (the pool fingerprint can change before this thread
        runs). Coalesced events for this provider run here; an event that
        reached another provider is handed to that provider's own scheduler,
        so its shutdown tracks (joins) the thread that runs it."""
        while True:
            try:
                if op == "archive":
                    self._skill_archive_once(skill_name, roots)
                else:
                    self._skill_ingest_once(skill_name, roots)
            except Exception as e:
                logger.warning("on_skill_lifecycle: %s of '%s' failed: %s", op, skill_name, e)
            with _LIFECYCLE_LOCK:
                nxt = _LIFECYCLE_PENDING.pop(key, None)
                again = nxt is not None and nxt[0] is self and not self._write_stop.is_set()
                if not again:
                    _LIFECYCLE_IN_FLIGHT.discard(key)
            if again and nxt is not None:
                _prov, op, roots = nxt
                continue
            if nxt is not None and nxt[0] is not self:
                nxt[0]._schedule_skill_lifecycle(skill_name, nxt[1], nxt[2])
            elif nxt is not None:
                logger.info("on_skill_lifecycle: provider shutting down; not processing '%s'", skill_name)
            return

    @staticmethod
    def _find_skill_file(roots: list[Path], skill_name: str) -> Path | None:
        """The live <skill_name>/SKILL.md under the roots, at any depth, as the
        bulk sync's discovery walks them (hidden trees skipped, each real
        directory entered once). A file that resolves into an ignored tree
        such as .archive/ (a link to an archived skill) is not live."""
        if not _SKILL_NAME_RE.match(skill_name or ""):
            return None
        live = _sync_skills_module().is_live_skill_file
        for root in roots:
            if not root.is_dir():
                continue
            candidate = root / skill_name / "SKILL.md"
            if live(candidate):
                return candidate
            for sub in _skill_md_matches(root, skill_name):
                if live(sub):
                    return sub
        for root in roots:
            if not root.is_dir():
                continue
            for cur_root, dirnames, _files in _sync_skills_module().walk_skill_tree(root):
                if skill_name in dirnames:
                    sub = Path(cur_root) / skill_name / "SKILL.md"
                    if live(sub):
                        return sub
        return None

    def _skill_ingest_once(self, skill_name: str, roots: list[Path]) -> None:
        """Replace this host's copy of one skill (full mode) in one transaction;
        skip the write when the stored hash already matches the file.

        The identity is the parsed skill name (the frontmatter name, else the
        directory name), exactly as the bulk sync stores it; the event name only
        locates the file."""
        skill_file = self._find_skill_file(roots, skill_name)
        if skill_file is None:
            logger.debug("on_skill_lifecycle: local SKILL.md not found for '%s'; skipping live ingest", skill_name)
            return
        _sync = _sync_skills_module()
        skill_info = _sync.parse_skill_file(skill_file)
        if not skill_info:
            return
        name = str(getattr(skill_info, "name", "") or "")
        if not _SKILL_NAME_RE.match(name):
            logger.warning("on_skill_lifecycle: skill '%s' has an invalid name %r; not stored", skill_name, name)
            return
        if name != skill_name:
            logger.debug("on_skill_lifecycle: '%s' is stored as '%s' (its frontmatter name)", skill_name, name)
        host_id = get_host_id()
        payload_rows = _sync.format_skill_payload(skill_info, mode="full", host=host_id)
        if not payload_rows:
            return
        current_hash = getattr(skill_info, "hash", None) or getattr(skill_info, "content_hash", "")
        own_binds = _sync.own_skill_rows_binds(host_id, name)
        if self._pool is None:
            logger.info("on_skill_lifecycle: provider closed; skipping upsert of '%s'", name)
            return
        # Hash check, DELETE of this host's rows and INSERT share one
        # transaction: a failed insert rolls back and the previous rows stay.
        # The checked acquire reads the pool once and drops a broken session.
        with self._acquire_checked() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT json_value(metadata, '$.hash'), json_value(metadata, '$.path'), "  # nosec B608
                        f"json_value(metadata, '$.total_chunks') FROM {self._table} "
                        "WHERE memory_id = :1 AND role = 'skill'",
                        (f"skill:{host_id}:{name}:c0",),
                    )
                    row = cur.fetchone()
                    stored_path = row[1] if row is not None and len(row) > 1 else None
                    # A meta-only copy (no total_chunks) is not current for this full write.
                    stored_full = row is not None and (len(row) < 3 or row[2] is not None)
                    if stored_full and _sync.copy_is_current(row[0], stored_path, current_hash, skill_info.path, roots):
                        logger.debug("on_skill_lifecycle: skill '%s' unchanged (%s); skipping", name, current_hash)
                        return
                    if _sync.live_elsewhere(stored_path, skill_file, roots):
                        logger.warning(
                            "on_skill_lifecycle: skill '%s' of host '%s' is stored from %s, which still exists "
                            "outside this profile's skill dirs; not replacing it (set HERMES_HOST_ID per "
                            "profile to keep separate copies)", name, host_id, stored_path,
                        )
                        return
                    cur.execute(f"DELETE FROM {self._table} WHERE {_sync.OWN_SKILL_ROWS_SQL}", own_binds)
                backfill = self._insert_many(payload_rows, tx_conn=conn)
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except Exception as rb_e:
                    logger.debug("on_skill_lifecycle rollback failed: %s", rb_e)
                raise
        for mid, cnt in backfill or []:
            self._queue_embed_backfill(mid, cnt)
        logger.info("on_skill_lifecycle: ingested skill '%s' (%d chunks)", name, len(payload_rows))

    def _skill_archive_once(self, skill_name: str, roots: list[Path]) -> None:
        """Delete this host's copy of an archived skill, unless a SKILL.md for
        it is still live under the roots. The copy is found by its id
        (skill:<host>:<name>:c0) or by its stored path (.../<name>/SKILL.md,
        for a skill whose frontmatter name differs from its directory). A copy
        whose stored SKILL.md still exists (and does not resolve into an
        ignored tree such as .archive/) is kept, wherever it lies (a live
        skill of this profile under another name, or another profile's copy),
        unless it is the target of the symlink this archive moved into
        .archive/. Other hosts' copies and the host-less shared corpus are
        never touched."""
        if not _SKILL_NAME_RE.match(skill_name or ""):
            return
        if self._find_skill_file(roots, skill_name) is not None:
            return
        _sync = _sync_skills_module()
        host_id = get_host_id()
        own_binds = _sync.own_skill_rows_binds(host_id, skill_name)
        if self._pool is None:
            return
        deleted = 0
        removed: list[str] = []
        kept: list[str] = []
        with self._acquire_checked() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT memory_id, json_value(metadata, '$.skill_name'), json_value(metadata, '$.path') "  # nosec B608
                        f"FROM {self._table} WHERE role = 'skill' AND memory_id LIKE :own ESCAPE '\\' "
                        "AND memory_id LIKE '%:c0' "
                        "AND (memory_id = :c0 OR json_value(metadata, '$.path') LIKE :tail ESCAPE '\\')",
                        {
                            "own": _sync.like_escape(f"skill:{host_id}:") + "%",
                            "c0": f"skill:{host_id}:{skill_name}:c0",
                            "tail": "%" + _sync.like_escape(f"{os.sep}{skill_name}{os.sep}SKILL.md"),
                        },
                    )
                    candidates = list(cur.fetchall() or [])
                    for cand in candidates:
                        mid = str(cand[0] or "")
                        sname = str(cand[1] or "") if len(cand) > 1 else ""
                        spath = cand[2] if len(cand) > 2 else None
                        if not sname:
                            m = re.match(r"^skill:[^:]+:([^:]+):c0$", mid)
                            sname = m.group(1) if m else ""
                        if not _SKILL_NAME_RE.match(sname):
                            continue
                        if _sync.is_live_skill_file(spath) and not _sync.archived_link_target(spath, roots, skill_name):
                            kept.append(f"{sname} ({spath})")
                            continue
                        cur.execute(
                            f"DELETE FROM {self._table} WHERE {_sync.OWN_SKILL_ROWS_SQL}",  # nosec B608
                            _sync.own_skill_rows_binds(host_id, sname),
                        )
                        deleted += cur.rowcount or 0
                        removed.append(sname)
                    if not candidates:
                        # No meta row (partial legacy write): clear any chunk rows under the id.
                        cur.execute(f"DELETE FROM {self._table} WHERE {_sync.OWN_SKILL_ROWS_SQL}", own_binds)  # nosec B608
                        deleted += cur.rowcount or 0
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
        logger.info(
            "on_skill_lifecycle: archived skill '%s': removed %d rows of host '%s' (%s)%s",
            skill_name, deleted, host_id, ", ".join(removed) or "no copy",
            f"; kept, SKILL.md still exists: {', '.join(kept)}" if kept else "",
        )

    def _load_skill_fetch(self, skill_name: str, path: str = "", variant: int | None = None, host: str = "") -> str:
        """Load and reconstruct a skill document from its constituent chunks in the shared corpus.
        Copies are keyed by (host, path); the DB fetch is bounded per copy."""
        skill_name = (skill_name or "").strip()
        if not skill_name or not re.match(r"^[A-Za-z0-9._-]+$", skill_name):
            return tool_error(f"Invalid skill name '{skill_name}'. Skill name must match ^[A-Za-z0-9._-]+$")

        if not self._pool:
            return tool_error(f"Oracle memory not initialized: {self._init_error or self.unavailable_reason()}")

        try:
            # At most _SKILL_FETCH_MAX_CHUNKS + 2 rows per copy (c0, the cap and
            # one more to detect truncation). CLOBs arrive as str in the same
            # round trip (_execute_lobs_as_str).
            sql = f"""
                SELECT idx, content, ref, total, metadata, memory_id
                FROM (
                    SELECT to_number(regexp_substr(memory_id, '[0-9]+$', 1, 1)) idx,
                           content,
                           json_value(metadata, '$.reference_file') ref,
                           json_value(metadata, '$.total_chunks') total,
                           metadata,
                           memory_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY json_value(metadata, '$.host'), json_value(metadata, '$.path')
                               ORDER BY to_number(regexp_substr(memory_id, '[0-9]+$', 1, 1)) NULLS FIRST, memory_id
                           ) rn
                    FROM {self._table}
                    WHERE role = 'skill' AND json_value(metadata, '$.skill_name') = :s
                )
                WHERE rn <= :cap
                ORDER BY idx NULLS FIRST
            """
            fetch_cap = max(1, self._tune("skill_fetch_max_chunks")) + 2

            def _fetch_skill_rows(conn):
                try:
                    conn.call_timeout = self._search_ms
                except Exception:
                    pass
                with conn.cursor() as cur:
                    cur.arraysize = min(fetch_cap * 2, 1000)
                    cur.prefetchrows = cur.arraysize + 1
                    _execute_lobs_as_str(cur, sql, {"s": skill_name, "cap": fetch_cap})
                    return cur.fetchall()

            try:
                with self._pool.acquire() as conn:
                    rows = _fetch_skill_rows(conn)
            except Exception as e:
                err_str = str(e).upper()
                if any(k in err_str for k in ("ORA-02396", "DPY-3015", "DPY-4024", "ORA-03113", "ORA-03114", "ORA-03135", "ORA-01012", "NOT CONNECTED", "SESSION KILLED")):
                    logger.warning("Stale connection during skill fetch (%s); closing, re-acquiring and retrying once", e)
                    with self._pool.acquire() as conn2:
                        rows = _fetch_skill_rows(conn2)
                else:
                    raise

            if not rows:
                return tool_error(f"skill '{skill_name}' not in corpus — use action=list to search")

            variants_map: dict[tuple[str, str], dict[str, Any]] = {}
            parsed_rows = []
            for r in rows:
                idx, content, ref, total, md_raw, mid = r[0], r[1], r[2], r[3], r[4], r[5]
                if not re.search(r":c\d+$", str(mid or "")):
                    idx = None
                md = _metadata_dict(md_raw)
                p = md.get("path") or ""
                h = md.get("host") or ""
                if p and (h, p) not in variants_map:
                    variants_map[(h, p)] = {
                        "path": p,
                        "hash": md.get("hash") or "",
                        "mtime": md.get("mtime") or "",
                        "host": h,
                    }
                if isinstance(content, (bytes, bytearray)):
                    content = content.decode("utf-8", "replace")
                elif content is not None and not isinstance(content, str):
                    content = _lob_text(content)
                parsed_rows.append({
                    "idx": idx,
                    "content": content or "",
                    "ref": ref,
                    "total": total,
                    "metadata": md,
                    "memory_id": mid,
                })

            # Variants in a stable order: (host, path); legacy (host-less) first.
            unique_variants = [variants_map[k] for k in sorted(variants_map)]
            if len(unique_variants) > 1:
                selected: dict[str, Any] | None = None
                if path or host:
                    matching = [
                        v for v in unique_variants
                        if (not path or v["path"] == path) and (not host or v["host"] == host)
                    ]
                    if not matching:
                        avail = ", ".join(f"{v['host'] or '(shared)'}:{v['path']}" for v in unique_variants)
                        return tool_error(
                            f"No variant of skill '{skill_name}' matches path='{path}' host='{host}'. Available: {avail}"
                        )
                    if len(matching) > 1:
                        return tool_error(
                            f"path '{path}' matches {len(matching)} copies of skill '{skill_name}' on different hosts; "
                            "pass 'host' or 'variant' as well"
                        )
                    selected = matching[0]
                elif variant is not None:
                    if not (0 <= variant < len(unique_variants)):
                        return tool_error(f"Variant index {variant} out of range (0-{len(unique_variants)-1}) for skill '{skill_name}'")
                    selected = unique_variants[variant]
                else:
                    return json.dumps({
                        "status": "multiple_variants",
                        "skill": skill_name,
                        "variants": [
                            {"variant": i, "host": v["host"], "path": v["path"], "hash": v["hash"], "mtime": v["mtime"]}
                            for i, v in enumerate(unique_variants)
                        ],
                        "message": f"Multiple variants exist for skill '{skill_name}'. Please specify 'variant' (or 'path' and 'host') to select one.",
                    }, default=_json_default)
                if selected is not None:
                    parsed_rows = [
                        r for r in parsed_rows
                        if r["metadata"].get("path") == selected["path"]
                        and (r["metadata"].get("host") or "") == selected["host"]
                    ]

            meta_row = None
            body_chunks: list[tuple[int, str]] = []
            ref_chunks: dict[str, list[tuple[int, str]]] = {}

            for r in parsed_rows:
                idx = r["idx"]
                ref = r["ref"]
                content = r["content"]
                if idx is None or idx == 0:
                    if meta_row is None or idx == 0:
                        meta_row = r
                elif ref:
                    if ref not in ref_chunks:
                        ref_chunks[ref] = []
                    ref_chunks[ref].append((int(idx), content))
                else:
                    body_chunks.append((int(idx), content))

            body_chunks.sort(key=lambda x: x[0])
            for ref in ref_chunks:
                ref_chunks[ref].sort(key=lambda x: x[0])

            total_retrieved_chunks = len(body_chunks) + sum(len(c) for c in ref_chunks.values())
            status = "ok"

            if total_retrieved_chunks > self._tune("skill_fetch_max_chunks"):
                status = "truncated"
                body_chunks = body_chunks[:self._tune("skill_fetch_max_chunks")]

            meta_md = meta_row["metadata"] if meta_row else {}
            category = meta_md.get("category") or (parsed_rows[0]["metadata"].get("category") if parsed_rows else "general")
            source_path = meta_md.get("path") or (parsed_rows[0]["metadata"].get("path") if parsed_rows else "")
            meta_hash = meta_md.get("hash") or ""
            mtime_iso = meta_md.get("mtime") or ""
            total_chunks_decl = int(meta_md.get("total_chunks") or (len(body_chunks) + 1))

            if not body_chunks and not ref_chunks:
                status = "meta_only"
                meta_text = meta_row["content"] if meta_row else ""
                return json.dumps({
                    "status": status,
                    "skill": skill_name,
                    "category": category,
                    "source_host_path": source_path,
                    "total_chunks": 1,
                    "chunk_count": 1,
                    "word_count": 0,
                    "markdown": meta_text,
                    "references": {},
                    "content_hash": meta_hash,
                    "mtime": mtime_iso,
                }, default=_json_default)

            raw_body_chunks = [c[1] for c in body_chunks]
            markdown = reassemble_chunks(raw_body_chunks)

            references: dict[str, str] = {}
            for ref_path, chunks_list in ref_chunks.items():
                raw_ref_chunks = [c[1] for c in chunks_list]
                references[ref_path] = reassemble_chunks(raw_ref_chunks)

            return json.dumps({
                "status": status,
                "skill": skill_name,
                "category": category,
                "source_host_path": source_path,
                "total_chunks": total_chunks_decl,
                "chunk_count": total_retrieved_chunks,
                "word_count": len(markdown.split()),
                "markdown": markdown,
                "references": references,
                "content_hash": meta_hash,
                "mtime": mtime_iso,
            }, default=_json_default)
        except Exception as e:
            logger.exception("oracle_load_skill fetch failed: %s", e)
            return tool_error(f"Failed to fetch skill '{skill_name}': {e}")

    def _load_skill_list(self, filter_str: str = "", offset: int = 0) -> str:
        """List skills from the shared Oracle skill corpus with substring filtering and pagination."""
        if not self._pool:
            return tool_error(f"Oracle memory not initialized: {self._init_error or self.unavailable_reason()}")

        offset = max(0, int(offset or 0))
        page_size = 500

        try:
            # One row per skill name (paging is over distinct names, so a page
            # is never short and never repeats a name), with the description
            # from one meta row and the number of stored copies.
            filt = (filter_str or "").strip()
            where_filter = ""
            binds: dict[str, Any] = {"o": offset, "limit": page_size + 1}
            if filt:
                where_filter = (
                    "AND (UPPER(json_value(metadata, '$.skill_name')) LIKE UPPER(:f) ESCAPE '\\' "
                    "OR UPPER(content) LIKE UPPER(:f) ESCAPE '\\')"
                )
                # The filter is a literal substring: % and _ are escaped.
                binds["f"] = "%" + _sync_skills_module().like_escape(filt) + "%"
            sql = f"""
                SELECT name, category, content, copies
                FROM (
                    SELECT json_value(metadata, '$.skill_name') AS name,
                           json_value(metadata, '$.category') AS category,
                           content,
                           COUNT(*) OVER (PARTITION BY json_value(metadata, '$.skill_name')) AS copies,
                           ROW_NUMBER() OVER (PARTITION BY json_value(metadata, '$.skill_name') ORDER BY memory_id) AS rn
                    FROM {self._table}
                    WHERE role = 'skill'
                      AND (memory_id LIKE '%:c0' OR regexp_like(memory_id, '^skill:[^:]+$'))
                      AND json_value(metadata, '$.skill_name') IS NOT NULL
                      {where_filter}
                )
                WHERE rn = 1
                ORDER BY name
                OFFSET :o ROWS FETCH FIRST :limit ROWS ONLY
            """

            def _run_list(conn):
                try:
                    conn.call_timeout = self._search_ms
                except Exception:
                    pass
                with conn.cursor() as cur:
                    cur.arraysize = page_size + 1
                    cur.prefetchrows = page_size + 2
                    _execute_lobs_as_str(cur, sql, binds)
                    rows = cur.fetchall()
                more = len(rows) > page_size
                skills_out = []
                for row in rows[:page_size]:
                    sname, scat, scontent = row[0], row[1] or "general", row[2] or ""
                    if not isinstance(scontent, str):
                        scontent = _lob_text(scontent)
                    item: dict[str, Any] = {
                        "name": sname,
                        "category": scat,
                        "description": _extract_skill_description(scontent, sname),
                    }
                    copies = row[3] if len(row) > 3 else None
                    if isinstance(copies, (int, float)) and copies > 1:
                        item["copies"] = int(copies)
                    skills_out.append(item)
                return json.dumps({
                    "count_in_page": len(skills_out),
                    "offset": offset,
                    "more": more,
                    "skills": skills_out,
                }, default=_json_default)

            try:
                with self._pool.acquire() as conn:
                    return _run_list(conn)
            except Exception as e:
                err_str = str(e).upper()
                if any(k in err_str for k in ("ORA-02396", "DPY-3015", "DPY-4024", "ORA-03113", "ORA-03114", "ORA-03135", "ORA-01012", "NOT CONNECTED", "SESSION KILLED")):
                    logger.warning("Stale connection during skill list (%s); closing, re-acquiring and retrying once", e)
                    with self._pool.acquire() as conn2:
                        return _run_list(conn2)
                else:
                    raise
        except Exception as e:
            logger.exception("oracle_load_skill list failed: %s", e)
            return tool_error(f"Failed to list skills: {e}")

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
        handler = self._TOOL_HANDLERS.get(tool_name)
        if handler is None:
            return tool_error(f"Unknown tool: {tool_name}")
        try:
            return getattr(self, handler)(args)
        except Exception as e:
            logger.exception("oracle tool %s failed", tool_name)
            return tool_error(str(e))

    # One method per tool. Each returns the tool's JSON string; an exception
    # is logged and turned into a tool error by handle_tool_call.
    _TOOL_HANDLERS = {
        "oracle_search": "_tool_search",
        "oracle_add": "_tool_add",
        "oracle_update": "_tool_update",
        "oracle_get": "_tool_get",
        "oracle_delete": "_tool_delete",
        "oracle_chunk": "_tool_chunk",
        "oracle_sync_skills": "_tool_sync_skills",
        "oracle_load_skill": "_tool_load_skill",
    }

    def _tool_search(self, args: dict[str, Any]) -> str:
        """oracle_search: Hybrid, vector or text search; a timeout answers with an empty result."""
        try:
            search_tags = _normalize_tags(args.get("tags"))
        except ValueError as ve:
            return tool_error(str(ve))
        try:
            rows = self._search(
                args.get("query") or "",
                top_k=int(args.get("top_k") or 8),
                mode=args.get("mode") or "hybrid",
                target=args.get("target") or "any",
                session_id=args.get("session_id"),
                agent_id=args.get("agent_id"),
                tags=search_tags or None,
                tag_match=str(args.get("tag_match") or "all"),
            )
            if not rows:
                return json.dumps(
                    {"result": "No relevant memories found.", "count": 0}
                )
            return json.dumps(_search_tool_payload(rows), default=_json_default)
        except Exception as e:
            is_timeout = _is_timeout_error(e) or "TimeoutError" in str(e)
            if is_timeout:
                logger.warning(
                    "oracle_search query timed out after %ss (%s); clean fallback to empty results",
                    self._search_secs,
                    e,
                )
                return json.dumps(
                    {
                        "result": f"No relevant memories found (oracle_search query timed out after {self._search_secs:.0f}s).",
                        "count": 0,
                        "timed_out": True,
                    }
                )
            raise

    def _tool_add(self, args: dict[str, Any]) -> str:
        """oracle_add: Store one fact, a batch of facts, or upsert a row with a caller-chosen memory_id."""
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
        if len(facts_to_add) > _ADD_MAX_FACTS:
            return tool_error(
                f"too many facts in one call ({len(facts_to_add)}; max {_ADD_MAX_FACTS}): split the batch"
            )
        too_long = [i for i, f in enumerate(facts_to_add) if len(f) > _MAX_FACT_CHARS]
        if too_long:
            return tool_error(
                f"fact {too_long[0]} is {len(facts_to_add[too_long[0]])} characters (max {_MAX_FACT_CHARS}); "
                "store long text with oracle_chunk"
            )

        try:
            add_tags = _normalize_tags(args.get("tags"))
            req_mid = args.get("memory_id")
            req_mid = _normalize_memory_id(req_mid) if req_mid not in (None, "") else None
        except ValueError as ve:
            return tool_error(str(ve))
        add_meta: dict[str, Any] = {"kind": "tool"}
        if add_tags:
            add_meta["tags"] = add_tags

        if req_mid is not None and req_mid.lower().startswith(_SKILL_ID_PREFIX):
            return tool_error(
                f"memory_id prefix '{_SKILL_ID_PREFIX}' is reserved for the skill corpus "
                "(written by oracle_sync_skills)"
            )
        if req_mid is not None:
            if len(facts_to_add) != 1:
                return tool_error(f"memory_id can only be used with a single fact (got {len(facts_to_add)})")
            return json.dumps(
                self._upsert_fact(
                    req_mid,
                    facts_to_add[0],
                    target=target,
                    tags=add_tags if args.get("tags") is not None else None,
                    metadata=add_meta,
                )
            )

        if len(facts_to_add) == 1:
            mid = f"fact_{uuid.uuid4().hex[:16]}"
            self._insert(
                memory_id=mid,
                session_id=self._session_id,
                role="fact",
                content=facts_to_add[0],
                target=target,
                metadata=add_meta,
            )
            out: dict[str, Any] = {"result": "Fact stored.", "memory_id": mid}
            if add_tags:
                out["tags"] = add_tags
            return json.dumps(out)
        else:
            # One array insert, committed once: either every fact is
            # stored (and its id returned) or none is.
            mids = [f"fact_{uuid.uuid4().hex[:16]}" for _ in facts_to_add]
            self._insert_many([
                {
                    "memory_id": mid,
                    "session_id": self._session_id,
                    "role": "fact",
                    "content": f,
                    "target": target,
                    "metadata": add_meta,
                }
                for mid, f in zip(mids, facts_to_add)
            ])
            return json.dumps({
                "result": f"Batch facts stored ({len(mids)}).",
                "memory_ids": mids,
                "count": len(mids),
            })

    def _tool_update(self, args: dict[str, Any]) -> str:
        """oracle_update: Replace a row's text and/or tags."""
        mid = str(args.get("memory_id") or "").strip()
        content = (args.get("content") or "").strip()
        has_tags = args.get("tags") is not None
        if not mid or (not content and not has_tags):
            return tool_error("memory_id and content (or tags) required")
        if len(content) > _MAX_FACT_CHARS:
            return tool_error(f"content is {len(content)} characters (max {_MAX_FACT_CHARS})")
        if mid.lower().startswith(_SKILL_ID_PREFIX):
            return tool_error(_SKILL_ROW_READONLY)
        try:
            upd_tags = _normalize_tags(args.get("tags")) if has_tags else None
        except ValueError as ve:
            return tool_error(str(ve))
        n = self._update_row(mid, content or None, tags=upd_tags)
        if not n:
            if self._is_skill_row(mid):
                return tool_error(_SKILL_ROW_READONLY)
            return tool_error(f"Memory not found: {mid}")
        upd_out: dict[str, Any] = {"result": "updated", "memory_id": mid}
        if upd_tags is not None:
            upd_out["tags"] = upd_tags
        return json.dumps(upd_out)

    def _tool_get(self, args: dict[str, Any]) -> str:
        """oracle_get: Exact read by memory_id(s) and/or tags."""
        try:
            ids: list[str] = []
            raw_ids = ([args["memory_id"]] if args.get("memory_id") else []) + _id_list(args.get("memory_ids"))
            for raw in raw_ids:
                norm_id = _normalize_memory_id(raw)
                if norm_id not in ids:
                    ids.append(norm_id)
            if len(ids) > _GET_MAX_LIMIT:
                return tool_error(f"too many memory_ids (max {_GET_MAX_LIMIT})")
            get_tags = _normalize_tags(args.get("tags"))
            # memory_id is unique, so an id lookup never needs more than len(ids) rows.
            default_limit = len(ids) if ids else _GET_DEFAULT_LIMIT
            eff_limit = max(1, min(int(args.get("limit") or default_limit), _GET_MAX_LIMIT))
            rows = self._get_rows(
                memory_ids=ids or None,
                tags=get_tags or None,
                tag_match=str(args.get("tag_match") or "all"),
                session_id=args.get("session_id"),
                agent_id=args.get("agent_id"),
                target=args.get("target"),
                limit=eff_limit,
            )
        except ValueError as ve:
            return tool_error(str(ve))
        found = {r["memory_id"] for r in rows}
        got: dict[str, Any] = {"results": rows, "count": len(rows)}
        if len(rows) >= eff_limit and (not ids or len(ids) > eff_limit):
            # Cut off by `limit`: unreturned ids may exist, so do not call them missing.
            got["truncated"] = True
        else:
            missing = [m for m in ids if m not in found]
            if missing:
                got["missing"] = missing
        if not rows:
            got["result"] = "No matching memories."
        return json.dumps(got, default=_json_default)

    def _tool_delete(self, args: dict[str, Any]) -> str:
        """oracle_delete: Delete one row by memory_id, or purge a session (see _purge_session)."""
        mid = str(args.get("memory_id") or "").strip()
        sid = str(args.get("session_id") or "").strip()
        if not mid and not sid:
            return tool_error("memory_id or session_id required")
        if mid and sid:
            return tool_error("pass memory_id OR session_id, not both (session_id purges a whole thread)")
        if sid and sid.lower() in _RESERVED_SESSION_IDS:
            return tool_error(
                f"session_id '{sid}' is shared by unrelated rows (skill corpus / rows without a "
                "session) and cannot be purged; delete individual rows by memory_id"
            )
        if mid and mid.lower().startswith(_SKILL_ID_PREFIX):
            return tool_error(_SKILL_ROW_READONLY)
        if sid:
            raw_max = args.get("max_rows")
            if raw_max is None or raw_max == "":
                max_rows = _PURGE_DEFAULT_MAX_ROWS
            else:
                ok = isinstance(raw_max, int) and not isinstance(raw_max, bool)
                if isinstance(raw_max, str) and raw_max.strip().isdigit():
                    raw_max, ok = int(raw_max.strip()), True
                if not ok or not 1 <= raw_max <= _PURGE_HARD_MAX_ROWS:
                    return tool_error(f"max_rows must be between 1 and {_PURGE_HARD_MAX_ROWS}")
                max_rows = raw_max
            return self._purge_session(sid, dry_run=_as_bool(args.get("dry_run", False)), max_rows=max_rows)
        with self._acquire_checked() as conn:
            with conn.cursor() as cur:
                # Skill-corpus rows (any id) belong to the skill sync, not the generic tools.
                query = (
                    f"DELETE /*+ NO_PARALLEL */ FROM {self._table} WHERE memory_id = :1"  # nosec B608
                    " AND NVL(role, 'fact') <> 'skill'"
                )
                cur.execute(
                    query,
                    (mid,),
                )
                n = cur.rowcount
                conn.commit()
        if not n:
            if self._is_skill_row(mid):
                return tool_error(_SKILL_ROW_READONLY)
            return tool_error(f"Memory not found: {mid}")
        return json.dumps({"result": "deleted", "memory_id": mid})

    def _tool_chunk(self, args: dict[str, Any]) -> str:
        """oracle_chunk: Chunk inline text or a file in the database and store the chunks."""
        content = (args.get("content") or "").strip()
        file_path = (args.get("file_path") or args.get("path") or "").strip()
        if not content and not file_path:
            return tool_error("Missing required parameter: content or file_path")
        try:
            max_words, overlap = _chunk_params(args.get("max_words"), args.get("overlap"))
        except ValueError as ve:
            return tool_error(str(ve))
        if file_path:
            try:
                p = Path(os.path.expanduser(os.path.expandvars(file_path))).resolve()
                why = _sensitive_path_reason(p)
                if why:
                    return tool_error(
                        f"Refusing to read {file_path} ({why}): chunked files are stored in the "
                        "shared memory database. Pass non-secret text via 'content' instead."
                    )
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
                    ids = self._chunk_and_store("", max_words, overlap, turns=structured_turns)
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
        ids = self._chunk_and_store(content, max_words, overlap)
        return json.dumps(
            {"result": "chunked", "count": len(ids), "memory_ids": ids}
        )

    def _tool_sync_skills(self, args: dict[str, Any]) -> str:
        """oracle_sync_skills: Index this host's skill directories into the skill corpus."""
        mode = str(args.get("mode") or "meta").strip().lower()
        if mode not in ("meta", "full"):
            return tool_error("mode must be 'meta' or 'full'")
        force = _as_bool(args.get("force", False))
        raw_dirs = args.get("dirs")
        if isinstance(raw_dirs, str):
            # A bare string would otherwise be iterated per character
            # ('~/x' -> '/', i.e. a walk of the whole filesystem).
            txt = raw_dirs.strip()
            try:
                raw_dirs = json.loads(txt) if txt.startswith("[") else ([txt] if txt else [])
            except ValueError:
                return tool_error("dirs: invalid JSON array")
        dirs: list[str | Path] | None = None
        if raw_dirs in (None, []):
            pass
        elif not isinstance(raw_dirs, list) or len(raw_dirs) > 20:
            return tool_error("dirs must be a list of up to 20 directory paths")
        else:
            dirs = []
            for d in raw_dirs:
                dp = Path(os.path.expanduser(os.path.expandvars(str(d)))).resolve()
                # Refuse broad roots: "/", $HOME and any ancestor of it, system/credential dirs.
                if _is_broad_scan_root(dp):
                    return tool_error(f"refusing to scan {d}: pass a specific skills directory")
                if not dp.is_dir():
                    return tool_error(f"not a directory: {d}")
                dirs.append(dp)
        batch_size = max(1, min(int(args.get("batch_size") or 50), 500))
        prune = _as_bool(args.get("prune", False))
        dry_run = _as_bool(args.get("dry_run", False))
        raw_limit = args.get("prune_limit")
        try:
            prune_limit = 100 if raw_limit in (None, "") else int(raw_limit)
        except (TypeError, ValueError):
            return tool_error("prune_limit must be an integer")
        if not 1 <= prune_limit <= 500:
            return tool_error("prune_limit must be between 1 and 500")
        res = self.sync_skills(
            mode=mode, force=force, dirs=dirs, batch_size=batch_size,
            prune=prune, prune_limit=prune_limit, dry_run=dry_run,
            replace_foreign=_as_bool(args.get("replace_foreign", False)),
        )
        return json.dumps(res)

    def _tool_load_skill(self, args: dict[str, Any]) -> str:
        """oracle_load_skill: Fetch a skill from the shared corpus, or list the skills in it."""
        action = (args.get("action") or "fetch").lower().strip()
        if action == "list":
            filter_str = args.get("filter") or ""
            offset = int(args.get("offset") or 0)
            return self._load_skill_list(filter_str=filter_str, offset=offset)
        elif action == "fetch":
            skill = args.get("skill") or ""
            path = args.get("path") or ""
            host = str(args.get("host") or "").strip()
            variant = args.get("variant")
            if variant is not None:
                try:
                    variant = int(variant)
                except (ValueError, TypeError):
                    variant = None
            return self._load_skill_fetch(skill, path=path, variant=variant, host=host)
        else:
            return tool_error(f"Invalid action '{action}' for oracle_load_skill. Expected 'fetch' or 'list'.")

    def _purge_session(self, sid: str, *, dry_run: bool, max_rows: int) -> str:
        """oracle_delete(session_id=...): delete the non-skill rows of one
        session, at most *max_rows* of them. A session with more rows is left
        untouched and the count is reported. dry_run only counts."""
        # Skill-corpus rows are shared across hosts: never purged by thread.
        where = "session_id = :sid AND (role IS NULL OR role <> 'skill')"
        binds = {"sid": sid}

        def too_many(n: int) -> str:
            msg = f"session_id '{sid}' has {n} rows, more than this call may delete ({max_rows}). Nothing was deleted."
            if n <= _PURGE_HARD_MAX_ROWS:
                msg += f" To delete them all, repeat with max_rows={n}."
            else:
                msg += f" No single call deletes more than {_PURGE_HARD_MAX_ROWS} rows; remove this session in SQL."
            return tool_error(msg)

        with self._acquire_checked() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT /*+ NO_PARALLEL */ COUNT(*) FROM {self._table} WHERE {where}", binds)  # nosec B608
                row = cur.fetchone()
                count = int(row[0]) if row else 0
                if dry_run:
                    return json.dumps({"result": "dry_run", "session_id": sid, "would_delete": count})
                if count > max_rows:
                    return too_many(count)
                cur.execute(f"DELETE /*+ NO_PARALLEL */ FROM {self._table} WHERE {where}", binds)  # nosec B608
                n = cur.rowcount
                if n > max_rows:  # rows were added after the count
                    conn.rollback()
                    return too_many(n)
                conn.commit()
        return json.dumps({"result": "thread_deleted", "session_id": sid, "deleted_count": n})

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
            size = max(1, max_words)
            step = max(1, size - max(0, overlap))
            for i in range(0, len(words), step):
                piece = " ".join(words[i : i + size])
                if piece:
                    chunks.append(piece)
                if i + size >= len(words):
                    break  # the rest is already inside this chunk
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

    def sync_skills(
        self,
        mode: str = "meta",
        force: bool = False,
        dirs: list[str | Path] | None = None,
        batch_size: int = 50,
        prune: bool = False,
        prune_limit: int = 100,
        dry_run: bool = False,
        replace_foreign: bool = False,
    ) -> dict[str, Any]:
        """Index this host's skills (the profile's skills dir and ~/.agents/skills,
        or *dirs*) under this host's identity; *prune* removes this host's
        skills that are gone from those roots; *replace_foreign* replaces this
        host's copies stored from SKILL.md files outside the profile's roots."""
        if not self._pool:
            raise RuntimeError(
                self._init_error or self.unavailable_reason() or "Oracle memory pool not initialized"
            )
        sync = _sync_skills_module()
        return sync.sync_skills_library(
            provider=self, mode=mode, force=force, dirs=dirs, dry_run=dry_run, batch_size=batch_size,
            prune=prune, prune_limit=prune_limit, replace_foreign=replace_foreign, host_tag=get_host_id(),
        )

    def shutdown(self) -> None:
        # Stop accepting new background writes, then drain in-flight and
        # pending work before closing the pool so no turn is lost to a
        # connection closed underneath a running writer.
        with _LIVE_PROVIDERS_LOCK:
            _LIVE_PROVIDERS.discard(self)
        self._write_stop.set()
        busy: list[threading.Thread] = []
        # 1. The turn writer feeds the backfill queue, so it goes first.
        wt = self._write_worker
        if wt and wt.is_alive():
            wt.join(timeout=30.0)
            if wt.is_alive():
                busy.append(wt)
        # 2. Skill-lifecycle writers (DELETE + re-INSERT of a skill, one
        # transaction; they queue backfills after their commit).
        # _write_stop is already set, so _start_sync_thread() refuses new ones and
        # this snapshot (same lock) contains every writer that did start.
        with self._sync_threads_lock:
            sync_threads = [t for t in self._sync_threads if t.is_alive()]
        deadline = time.monotonic() + 30.0
        for t in sync_threads:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
            if t.is_alive():
                busy.append(t)
        # 3. Drain the embedding backfill: with _write_stop set every pending
        # batch gets one attempt and no backoff (rows it cannot embed keep NULL
        # embeddings and are re-queued by the next startup scan).
        et = self._ensure_embed_worker()
        if et and et.is_alive():
            et.join(timeout=30.0)
            if et.is_alive():
                busy.append(et)
        # Prefetch and the startup backfill scan only read; do not hold
        # shutdown (or leak the pool) for them.
        for rt in (self._prefetch_thread, self._backfill_scan_thread):
            if rt and rt.is_alive():
                rt.join(timeout=3.0)
        if busy:
            # A writer still holds a pooled connection (an ingest call may run
            # for the whole write budget). Closing the pool under it raised
            # "list.remove(x): x not in list" and failed every queued row with
            # "pool not initialized", so keep the pool for it and finish the
            # teardown in the background once it is done. A re-initialize()
            # with the same configuration in the meantime re-arms the pool.
            logger.warning(
                "oracle shutdown: %d worker(s) still busy after their join budget; pool closes when they finish",
                len(busy),
            )
            pool = self._pool
            _spawn_thread(self._finish_shutdown_when_idle, args=(busy, pool), name="oracle-shutdown-finish").start()
            return
        with self._pool_lock:
            pool, self._pool = self._pool, None
        _retire_pool(pool, "shutdown")

    def _finish_shutdown_when_idle(self, threads: list[threading.Thread], pool: Any) -> None:
        """Deferred end of a busy shutdown(): wait for the busy workers (bounded
        by the longest call budget), then close the pool unless initialize()
        re-armed this provider with it in the meantime."""
        budget = max(_SEARCH_TIMEOUT_SECS, _ingest_timeout_secs()) + _pool_wait_timeout_ms() / 1000.0 + 30.0
        deadline = time.monotonic() + budget
        for t in threads:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        if any(t.is_alive() for t in threads):
            # Still holding a connection past every call budget: never close
            # the pool under it; process exit releases it.
            logger.warning("oracle shutdown: worker still busy after %.0fs; pool left open", budget)
            return
        with self._pool_lock:
            if not self._write_stop.is_set() or pool is None or self._pool is not pool:
                return  # re-armed by initialize(), or already replaced/closed
            self._pool = None
        _retire_pool(pool, "shutdown")

    def _start_sync_thread(self, thread: threading.Thread) -> bool:
        """Start a skill-lifecycle writer and record it so shutdown() waits for it.

        Starting and recording happen under one lock, and shutdown() sets
        _write_stop before it snapshots the list under the same lock, so a writer
        is either in that snapshot or refused here. Every listed thread was
        started under the lock, so pruning on is_alive() only drops finished ones.
        Returns False (thread not started) once shutdown has begun."""
        with self._sync_threads_lock:
            if self._write_stop.is_set():
                return False
            self._sync_threads = [t for t in self._sync_threads if t.is_alive()]
            thread.start()
            self._sync_threads.append(thread)
            return True


_safe_embed_text = OracleMemoryProvider._safe_embed_text
_safe_varchar_bytes = OracleMemoryProvider._safe_varchar_bytes


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
        try:
            skill_path = Path(__file__).parent / "skills" / "oracle-skill-corpus" / "SKILL.md"
            if skill_path.exists():
                ctx.register_skill(
                    "oracle-skill-corpus",
                    skill_path,
                    "Discover and materialize shared cross-server skills from Oracle 26ai ADB memory at runtime without requiring local disk installation",
                )
        except Exception as exc:
            logger.debug("Failed to register oracle-skill-corpus skill: %s", exc)
