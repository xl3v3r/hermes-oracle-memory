#!/usr/bin/env python3
"""Import conversation archives into Oracle 26ai memory.

The importer:

- walks each conversation's messages in their real order (created_at, then
  position in file),
- chunks each message independently, and
- stamps every chunk with turn_index / chunk_index / turn_id / conv_ref so the
  conversation can be reassembled verbatim with
  `ORDER BY turn_index, chunk_index` (per conv_ref).

Every chunk is stored with role "chunk" and target "memory", so oracle_search
and auto-recall return it whoever wrote the message. The speaker is kept in
metadata.sender.

Supported source formats (auto-detected):
- Claude / ChatGPT export: list of {"name", "summary", "uuid", "chat_messages": [...]}
- ShareGPT: {"conversations": [...]} or list of sessions with "conversations"
  or "chat_messages"
- A truncated JSON file: the complete leading objects are imported.

All rows go under one session id (default import-ordered-YYYYMMDD, UTC), so an
import can be found, and removed, as a unit.

    python3 scripts/import_conversations_ordered.py conversations.json
    python3 scripts/import_conversations_ordered.py --file export.zip --session-id archive-2026 --env-file <env file>
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

try:  # helpers shared by the scripts (scripts/_common.py)
    from . import _common as common
except ImportError:  # run as a script, or imported with this directory on sys.path
    import _common as common  # type: ignore[no-redef]

load_env = common.load_env

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("oracle-import-ordered")


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


def parse_json_safely(raw: str) -> list[Any]:
    """Parse complete JSON, or recover the complete leading objects of a
    truncated array. Returns a list of top-level values ([] when none)."""
    try:
        return [json.loads(raw)]
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    s = raw.strip()
    if s.startswith("["):
        s = s[1:].lstrip()
    results: list[Any] = []
    idx = 0
    while idx < len(s):
        while idx < len(s) and s[idx] in " \t\r\n,":
            idx += 1
        if idx >= len(s) or s[idx] == "]":
            break
        try:
            obj, idx = decoder.raw_decode(s, idx)
        except json.JSONDecodeError:
            break
        results.append(obj)
    return results


def load_archive(src: Path) -> list[dict[str, Any]]:
    """The conversations of an archive file (.json, or a .zip holding one)."""
    if src.suffix == ".zip":
        import zipfile

        with zipfile.ZipFile(src) as zf:
            names = [n for n in zf.namelist() if n.endswith(".json") and not n.startswith("__MACOSX")]
            if not names:
                raise ValueError(f"no .json inside {src}")
            raw = zf.read(sorted(names, key=lambda n: -zf.getinfo(n).file_size)[0]).decode("utf-8", errors="replace")
    else:
        raw = src.read_text(encoding="utf-8", errors="replace")
    values = parse_json_safely(raw)
    if len(values) == 1:
        return parse_conversations(values[0])
    # Recovered from a truncated array: the values are its leading elements.
    return parse_conversations(values)


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (s[:40] or fallback)


# Senders whose entries stand on their own (a summary export lists one summary
# per entry) instead of being turns of one dialogue.
_STANDALONE_SENDERS = frozenset({"", "system", "summary"})
_TITLE_RE = re.compile(r"^\s*(?:Conversation|Title)\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def _sender(msg: Any) -> str:
    if not isinstance(msg, dict):
        return ""
    return str(msg.get("sender") or msg.get("from") or msg.get("role") or "")


def _ordered_turns(messages: Any) -> list[dict[str, Any]]:
    """The messages as turns in their real order: by created_at when present
    (stable, so the file position breaks ties), undated messages last."""
    if not isinstance(messages, list):
        return []

    def sort_key(pair: tuple[int, Any]) -> tuple[int, str, int]:
        pos, m = pair
        ts = m.get("created_at") if isinstance(m, dict) else None
        return (0, str(ts), pos) if isinstance(ts, str) and ts else (1, "", pos)

    turns: list[dict[str, Any]] = []
    for pos, m in sorted(enumerate(messages), key=sort_key):
        text = _extract_turn_text(m)
        if not text:
            continue
        turn_id = str(m.get("uuid") or m.get("id") or "") if isinstance(m, dict) else ""
        turns.append({"sender": _sender(m), "text": text, "turn_id": turn_id or f"p{pos:05d}"})
    return turns


def _header_turn(item: dict[str, Any]) -> dict[str, Any] | None:
    """A conversation's title and summary as its first turn."""
    parts = []
    title = str(item.get("name") or item.get("title") or "")
    if title:
        parts.append(f"Conversation: {title}")
    if item.get("summary"):
        parts.append(f"Summary: {item['summary']}")
    if not parts:
        return None
    return {"sender": "system", "text": "\n\n".join(parts), "turn_id": "header"}


def _parse_session(item: dict[str, Any], fallback_ref: str) -> list[dict[str, Any]]:
    """The conversations in one archive entry."""
    title = str(item.get("name") or item.get("title") or "")
    header = _header_turn(item)
    messages = item.get("chat_messages")
    if not isinstance(messages, list):
        messages = item.get("conversations") if isinstance(item.get("conversations"), list) else None

    if messages is None:
        # A single message, or a session that has only a title or summary.
        text = _extract_turn_text(item)
        if text:
            turn_id = str(item.get("uuid") or item.get("id") or "m0")
            return [{"conv_ref": title or fallback_ref,
                     "turns": [{"sender": _sender(item), "text": text, "turn_id": turn_id}]}]
        return [{"conv_ref": title or fallback_ref, "turns": [header]}] if header else []

    standalone = not header and all(_sender(m).lower() in _STANDALONE_SENDERS for m in messages)
    if standalone:
        # A summary export: every entry is its own document, named by its
        # "Conversation: <title>" line when it has one.
        docs = []
        for pos, m in enumerate(messages):
            text = _extract_turn_text(m)
            if not text:
                continue
            found = _TITLE_RE.search(text)
            ref = found.group(1).strip() if found else f"{fallback_ref}-{pos:05d}"
            turn_id = str(m.get("uuid") or m.get("id") or "m0") if isinstance(m, dict) else "m0"
            docs.append({"conv_ref": ref, "turns": [{"sender": _sender(m), "text": text, "turn_id": turn_id}]})
        return docs

    turns = _ordered_turns(messages)
    if header:
        turns.insert(0, header)
    return [{"conv_ref": title or fallback_ref, "turns": turns}] if turns else []


def parse_conversations(data: Any) -> list[dict[str, Any]]:
    """Parse the archive into conversations, each an ordered list of turns.

    Returns [{"conv_ref": str, "turns": [{"sender", "text", "turn_id"}]}].
    A conversation's title and summary, when present, are its first turn.
    """
    if isinstance(data, dict):
        return _parse_session(data, "conversation")
    out: list[dict[str, Any]] = []
    if isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, dict):
                out.extend(_parse_session(item, f"conv{i:04d}"))
            elif isinstance(item, str) and item.strip():
                out.append({"conv_ref": f"conv{i:04d}",
                            "turns": [{"sender": "", "text": item.strip(), "turn_id": "m0"}]})
    return out


def run(args: argparse.Namespace) -> int:
    src = Path(os.path.expanduser(args.file))
    if not src.is_file():
        logger.error("file not found: %s", src)
        return 2
    load_env(getattr(args, "env_file", None))

    logger.info("loading %s (%.1f MB)...", src, src.stat().st_size / 1e6)
    try:
        convs = load_archive(src)
    except ValueError as e:
        logger.error("%s", e)
        return 2
    if args.limit_conversations:
        convs = convs[: args.limit_conversations]
    total_turns = sum(len(c["turns"]) for c in convs)
    logger.info("parsed %d conversations, %d turns", len(convs), total_turns)
    if not convs:
        logger.error("no conversations parsed; aborting")
        return 2

    # Loaded only now: a missing file or an empty archive needs no plugin and
    # no database.
    provider = common.load_plugin().OracleMemoryProvider()
    session_id = args.session_id or f"import-ordered-{datetime.now(timezone.utc):%Y%m%d}"
    provider.initialize(session_id=session_id, agent_identity="hermes")
    if not provider._pool:
        logger.error("provider init failed: %s", provider._init_error)
        return 2
    logger.info("target session_id=%s table=%s onnx=%s", session_id, provider._table, provider._onnx_model)

    t0 = time.time()
    total_chunks = 0
    try:
        total_chunks = _import_all(provider, convs, args, total_turns, t0)
    finally:
        provider.shutdown()
    logger.info("DONE: %d conversations, %d turns, %d chunks in %.1f min -> session %s",
                len(convs), total_turns, total_chunks, (time.time() - t0) / 60, session_id)
    return 0


def _import_all(provider: Any, convs: list[dict[str, Any]], args: argparse.Namespace, total_turns: int, t0: float) -> int:
    """Chunk and store every conversation; returns the number of chunks."""
    total_chunks = 0
    done_turns = 0
    for ci, conv in enumerate(convs):
        conv_ref = (conv["conv_ref"] or f"conv{ci:04d}")[:512]
        slug = _slug(conv["conv_ref"], f"conv{ci:04d}")
        turns = []
        for turn in conv["turns"]:
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

    return total_chunks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("path", nargs="?", default="", help="conversation archive (.json or .zip)")
    ap.add_argument("--file", default="", help="the archive, as an option (same as the positional argument)")
    ap.add_argument("--session-id", default="", help="target session id (default: import-ordered-<date UTC>)")
    ap.add_argument("--max-words", type=int, default=80)
    ap.add_argument("--overlap", type=int, default=10)
    ap.add_argument("--limit-conversations", type=int, default=0, help="import only the first N (smoke test)")
    ap.add_argument("--env-file", default=None, help="load OCI_DB_* settings from this KEY=VALUE file (default: environment only)")
    args = ap.parse_args(argv)
    if bool(args.path) == bool(args.file):
        ap.error("give the archive once: as an argument or with --file")
    args.file = args.file or args.path
    try:
        return run(args)
    except KeyboardInterrupt:
        logger.warning("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
