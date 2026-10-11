#!/usr/bin/env python3
"""Bulk import conversation archives into Oracle 26ai Autonomous Database memory.

Supports:
- Single-session ShareGPT (dict with "conversations")
- Multi-session ShareGPT (list of sessions with "conversations")
- Claude / ChatGPT exports (list with "chat_messages")
- Plain text / markdown files
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path

# Add plugin parent directory to path if running directly
plugin_root = Path(__file__).resolve().parent.parent.parent
if str(plugin_root) not in sys.path:
    sys.path.insert(0, str(plugin_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("oracle-import")


def parse_conversations(data: object) -> list[dict[str, str]]:
    """Extract conversation turns as a flat list of dicts with role, content, sender."""
    extracted: list[dict[str, str]] = []

    if isinstance(data, dict):
        if "conversations" in data and isinstance(data["conversations"], list):
            for i, msg in enumerate(data["conversations"]):
                if isinstance(msg, dict):
                    sender = msg.get("from") or msg.get("role") or "unknown"
                    val = (msg.get("value") or msg.get("content") or msg.get("text") or "").strip()
                    if val and sender != "system":
                        extracted.append({
                            "sender": sender,
                            "role": "user" if sender in ("human", "user") else "assistant",
                            "content": f"[{sender.upper()}]: {val}",
                            "raw_text": val,
                        })
    elif isinstance(data, list):
        for session in data:
            if isinstance(session, dict):
                # ShareGPT multi-session
                convs = session.get("conversations")
                if isinstance(convs, list):
                    for msg in convs:
                        if isinstance(msg, dict):
                            sender = msg.get("from") or msg.get("role") or "unknown"
                            val = (msg.get("value") or msg.get("content") or msg.get("text") or "").strip()
                            if val and sender != "system":
                                extracted.append({
                                    "sender": sender,
                                    "role": "user" if sender in ("human", "user") else "assistant",
                                    "content": f"[{sender.upper()}]: {val}",
                                    "raw_text": val,
                                })
                # Claude / ChatGPT export
                chat_msgs = session.get("chat_messages")
                if isinstance(chat_msgs, list):
                    for msg in chat_msgs:
                        if isinstance(msg, dict):
                            sender = msg.get("sender") or msg.get("from") or "unknown"
                            val = (msg.get("text") or msg.get("content") or "").strip()
                            if val and sender != "system":
                                extracted.append({
                                    "sender": sender,
                                    "role": "user" if sender in ("human", "user") else "assistant",
                                    "content": f"[{sender.upper()}]: {val}",
                                    "raw_text": val,
                                })
                elif "value" in session or "content" in session:
                    sender = session.get("from") or session.get("role") or "unknown"
                    val = (session.get("value") or session.get("content") or "").strip()
                    if val and sender != "system":
                        extracted.append({
                            "sender": sender,
                            "role": "user" if sender in ("human", "user") else "assistant",
                            "content": f"[{sender.upper()}]: {val}",
                            "raw_text": val,
                        })
    return extracted


def main() -> int:
    parser = argparse.ArgumentParser(description="Import conversations into Oracle 26ai ADB memory")
    parser.add_argument("file", help="Path to JSON or text conversation archive")
    parser.add_argument("--batch-size", type=int, default=50, help="Batch size for database inserts (default 50)")
    parser.add_argument("--session-id", default=None, help="Custom session ID (defaults to auto-generated)")
    args = parser.parse_args()

    file_path = Path(os.path.expanduser(args.file)).resolve()
    if not file_path.is_file():
        logger.error("File not found: %s", file_path)
        return 1

    logger.info("Reading source file: %s (%d bytes)", file_path, file_path.stat().st_size)
    raw_content = file_path.read_text(encoding="utf-8", errors="replace")

    # Try parsing as JSON first
    turns: list[dict[str, str]] = []
    try:
        parsed_data = json.loads(raw_content)
        turns = parse_conversations(parsed_data)
        logger.info("Parsed %d structured conversation turns from JSON", len(turns))
    except json.JSONDecodeError:
        logger.info("Source is not JSON, treating as raw document")

    # Fallback for plain text or empty JSON parse
    if not turns:
        lines = [line.strip() for line in raw_content.splitlines() if line.strip()]
        for line in lines:
            turns.append({
                "sender": "user",
                "role": "user",
                "content": line,
                "raw_text": line,
            })
        logger.info("Extracted %d lines from text file", len(turns))

    if not turns:
        logger.warning("No content found to import")
        return 0

    try:
        from __init__ import OracleMemoryProvider
    except ImportError:
        import hermes_oracle_memory as _hom
        OracleMemoryProvider = _hom.OracleMemoryProvider

    provider = OracleMemoryProvider()
    session_id = args.session_id or f"import-{uuid.uuid4().hex[:8]}"
    provider.initialize(session_id=session_id)

    if not provider._pool:
        logger.error("Failed to connect to Oracle ADB: %s", provider._init_error or provider.unavailable_reason())
        return 1

    logger.info("Connected to Oracle ADB table: %s (session=%s)", provider._table, session_id)

    total = len(turns)
    imported = 0
    batch_size = max(1, args.batch_size)

    for i in range(0, total, batch_size):
        chunk = turns[i : i + batch_size]
        rows = []
        for idx, turn in enumerate(chunk):
            mid = f"imp_{uuid.uuid4().hex[:14]}"
            target = "user" if turn["role"] == "user" else "memory"
            rows.append({
                "memory_id": mid,
                "session_id": session_id,
                "role": turn["role"],
                "content": turn["content"],
                "target": target,
                "metadata": {
                    "source_file": file_path.name,
                    "turn_index": i + idx,
                    "sender": turn["sender"],
                },
            })

        provider._insert_many(rows)
        imported += len(rows)
        logger.info("Progress: imported %d / %d turns (%.1f%%)", imported, total, (imported / total) * 100)

    logger.info("Successfully imported %d conversation turns into Oracle 26ai ADB memory!", imported)
    return 0


if __name__ == "__main__":
    sys.exit(main())
