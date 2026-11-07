#!/usr/bin/env python3
"""Ordered re-import of conversation archives into Oracle 26ai memory.

Unlike the legacy chunker (which flattened every turn of every conversation
into one blob and chunked it blind), this importer:

- walks each conversation's messages in their real order (created_at, then
  position in file),
- chunks each message independently, and
- stamps every chunk with turn_index / chunk_index / turn_id / conv_ref so the
  conversation can be reassembled verbatim with
  `ORDER BY turn_index, chunk_index` (per conv_ref).

Supported source formats (auto-detected):
- ChatGPT export: list of {"name", "summary", "uuid", "chat_messages": [...]}
- ShareGPT: {"conversations": [...]} or list of sessions with "conversations"
  or "chat_messages"

Target: a NEW session id (default import-ordered-YYYYMMDD). The old unordered
session is left untouched so the switch is a rollback away until the new
session passes audit.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("oracle-import-ordered")


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


def _extract_turn_text(msg: Any) -> str:
    """Extract clean string text from a message payload."""
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


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (s[:40] or fallback)


def parse_conversations(data: Any) -> list[dict[str, Any]]:
    """Parse the archive into conversations, each an ordered list of turns.

    Returns [{"conv_ref": str, "turns": [{"sender", "text", "turn_id"}]}].
    """
    out: list[dict[str, Any]] = []

    def turns_from_messages(messages: Any) -> list[dict[str, Any]]:
        turns: list[dict[str, Any]] = []
        if not isinstance(messages, list):
            return turns
        # Real order first: sort by created_at when present, stable so the
        # file position breaks ties.
        indexed = list(enumerate(messages))
        def _sort_key(pair):
            pos, m = pair
            ts = m.get("created_at") if isinstance(m, dict) else None
            return (0, str(ts), pos) if isinstance(ts, str) and ts else (1, "", pos)
        indexed.sort(key=_sort_key)
        for pos, m in indexed:
            if isinstance(m, str) and m.strip():
                turns.append({"sender": "", "text": m.strip(), "turn_id": f"p{pos:05d}"})
                continue
            if not isinstance(m, dict):
                continue
            val = _extract_turn_text(m)
            if not val:
                continue
            sender = str(m.get("sender") or m.get("from") or m.get("role") or "")
            turn_id = str(m.get("uuid") or m.get("id") or f"p{pos:05d}")
            turns.append({"sender": sender, "text": val, "turn_id": turn_id})
        return turns

    def add_doc(conv_ref: str, messages: Any) -> None:
        turns = turns_from_messages(messages)
        if turns:
            out.append({"conv_ref": conv_ref, "turns": turns})

    if isinstance(data, dict):
        if "conversations" in data and isinstance(data["conversations"], list):
            if "name" in data or "summary" in data:
                title = str(data.get("name") or data.get("title") or "")
                header = []
                if title:
                    header.append(f"Conversation: {title}")
                if data.get("summary"):
                    header.append(f"Summary: {data['summary']}")
                msgs = ([{ "sender": "system", "text": "\n\n".join(header),
                           "turn_id": "header" }] + data["conversations"]) if header else data["conversations"]
                add_doc(title or "conversation", msgs)
            else:
                for m in data["conversations"]:
                    val = _extract_turn_text(m)
                    if val:
                        add_doc("conversation", [{"sender": m.get("from") or m.get("role") or "",
                                                  "text": val, "turn_id": str(m.get("uuid") or "m0")}])
        elif "chat_messages" in data:
            title = str(data.get("name") or data.get("title") or "")
            add_doc(title or "conversation", data["chat_messages"])
    elif isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, dict):
                title = str(item.get("name") or item.get("title") or "")
                summary = str(item.get("summary") or "")
                header = []
                if title:
                    header.append(f"Conversation: {title}")
                if summary:
                    header.append(f"Summary: {summary}")
                if "chat_messages" in item:
                    msgs = item["chat_messages"]
                    if header:
                        msgs = [{"sender": "system", "text": "\n\n".join(header),
                                 "turn_id": "header"}] + list(msgs)
                    add_doc(title or f"conv{i:04d}", msgs)
                elif "conversations" in item and isinstance(item["conversations"], list):
                    msgs = item["conversations"]
                    if header:
                        msgs = [{"sender": "system", "text": "\n\n".join(header),
                                 "turn_id": "header"}] + list(msgs)
                    add_doc(title or f"conv{i:04d}", msgs)
                else:
                    val = _extract_turn_text(item)
                    if val:
                        add_doc(title or f"conv{i:04d}",
                                [{"sender": item.get("from") or item.get("role") or "", "text": val,
                                  "turn_id": str(item.get("uuid") or "m0")}])
            elif isinstance(item, str) and item.strip():
                add_doc(f"conv{i:04d}", [{"sender": "", "text": item.strip(), "turn_id": "m0"}])
    return out


def run(args: argparse.Namespace) -> int:
    load_env()

    # The plugin runs inside the Hermes gateway, which provides the `agent`
    # runtime modules. Standalone scripts stub them; get_secret must be REAL
    # (env-backed) so the provider can read OCI_DB_* and connect.
    import types
    from unittest.mock import MagicMock

    def _env_secret(key: str, default: Any = None) -> Any:
        return os.environ.get(key, default)

    agent_mod = types.ModuleType("agent")
    mp_mod = types.ModuleType("agent.memory_provider")
    mp_mod.MemoryProvider = type("MemoryProvider", (), {})
    from dataclasses import dataclass

    @dataclass
    class RecallStatus:
        provider_label: str
        count: int

    mp_mod.RecallStatus = RecallStatus
    mp_mod.is_trivial_prompt = MagicMock(return_value=False)
    ss_mod = types.ModuleType("agent.secret_scope")
    ss_mod.get_secret = _env_secret
    agent_mod.memory_provider = mp_mod
    agent_mod.secret_scope = ss_mod
    sys.modules.setdefault("agent", agent_mod)
    sys.modules["agent.memory_provider"] = mp_mod
    sys.modules["agent.secret_scope"] = ss_mod
    tools_mod = sys.modules.get("tools") or types.ModuleType("tools")
    tr_mod = types.ModuleType("tools.registry")
    tr_mod.tool_error = MagicMock(return_value="error")
    tools_mod.registry = tr_mod
    sys.modules["tools"] = tools_mod
    sys.modules["tools.registry"] = tr_mod

    import importlib.util

    spec = importlib.util.spec_from_file_location("oracle_plugin", _repo_root / "__init__.py")
    assert spec is not None and spec.loader is not None
    oracle_plugin = importlib.util.module_from_spec(spec)
    sys.modules["oracle_plugin"] = oracle_plugin
    spec.loader.exec_module(oracle_plugin)
    OracleMemoryProvider = oracle_plugin.OracleMemoryProvider

    src = Path(args.file)
    if not src.is_file():
        logger.error("file not found: %s", src)
        return 2
    logger.info("loading %s (%.1f MB)...", src, src.stat().st_size / 1e6)
    if src.suffix == ".zip":
        import zipfile
        with zipfile.ZipFile(src) as zf:
            names = [n for n in zf.namelist() if n.endswith(".json") and not n.startswith("__MACOSX")]
            if not names:
                logger.error("no .json inside %s", src)
                return 2
            data = json.loads(zf.read(sorted(names, key=lambda n: -zf.getinfo(n).file_size)[0]))
    else:
        data = json.loads(src.read_text(encoding="utf-8", errors="replace"))

    convs = parse_conversations(data)
    if args.limit_conversations:
        convs = convs[: args.limit_conversations]
    total_turns = sum(len(c["turns"]) for c in convs)
    logger.info("parsed %d conversations, %d turns", len(convs), total_turns)
    if not convs:
        logger.error("no conversations parsed; aborting")
        return 2

    provider = OracleMemoryProvider()
    session_id = args.session_id or f"import-ordered-{datetime.now(timezone.utc):%Y%m%d}"
    provider.initialize(session_id=session_id, agent_identity="hermes")
    if not provider._pool:
        logger.error("provider init failed: %s", provider._init_error)
        return 2
    logger.info("target session_id=%s table=%s onnx=%s", session_id, provider._table, provider._onnx_model)

    t0 = time.time()
    total_chunks = 0
    done_turns = 0
    for ci, conv in enumerate(convs):
        conv_ref = (conv["conv_ref"] or f"conv{ci:04d}")[:512]
        slug = _slug(conv["conv_ref"], f"conv{ci:04d}")
        turns = []
        for ti, turn in enumerate(conv["turns"]):
            turn_id = f"{slug}#{turn['turn_id']}"[:64]
            turns.append({"sender": turn["sender"], "text": turn["text"],
                          "conv_ref": conv_ref, "turn_id": turn_id})
        ids = provider._chunk_and_store("", args.max_words, args.overlap, turns=turns)
        total_chunks += len(ids)
        done_turns += len(conv["turns"])
        if (ci + 1) % 25 == 0 or ci + 1 == len(convs):
            rate = done_turns / max(0.1, time.time() - t0)
            eta = (total_turns - done_turns) / max(0.1, rate)
            logger.info("  %d/%d conversations, %d turns, %d chunks (%.0f turns/s, eta %.0f min)",
                        ci + 1, len(convs), done_turns, total_chunks, rate, eta / 60)

    logger.info("DONE: %d conversations, %d turns, %d chunks in %.1f min -> session %s",
                len(convs), total_turns, total_chunks, (time.time() - t0) / 60, session_id)
    provider.shutdown()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", required=True, help="conversation archive (.json or .zip)")
    ap.add_argument("--session-id", default="", help="target session id (default: import-ordered-<date UTC>)")
    ap.add_argument("--max-words", type=int, default=80)
    ap.add_argument("--overlap", type=int, default=10)
    ap.add_argument("--limit-conversations", type=int, default=0, help="import only the first N (smoke test)")
    args = ap.parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        logger.warning("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
