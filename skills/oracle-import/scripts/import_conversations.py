#!/usr/bin/env python3
"""Bulk import conversation archives into Oracle 26ai Autonomous Database memory.

Supports:
- Single-session ShareGPT (dict with "conversations")
- Multi-session ShareGPT (list of sessions with "conversations")
- Claude / ChatGPT exports (list with "name", "summary", "chat_messages")
- Preprocessed conversation summaries (such as oracle_import.json)
- Truncated or malformed JSON streams via incremental decoding
- Plain text / markdown files
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

def _find_plugin_root(start: Path) -> Path:
    """Locate the plugin root directory containing plugin.yaml and __init__.py."""
    for parent in (start, *start.parents):
        if (parent / "plugin.yaml").exists() and (parent / "__init__.py").exists():
            return parent
    return start.parent.parent.parent.parent


# Add plugin parent directory to path if running directly
plugin_root = _find_plugin_root(Path(__file__).resolve())
if str(plugin_root) not in sys.path:
    sys.path.insert(0, str(plugin_root))


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("oracle-import")


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


def parse_conversations(data: object) -> list[dict[str, str]]:
    """Extract conversation turns as a flat list of dicts with role, content, sender."""
    extracted: list[dict[str, str]] = []

    def _determine_role(sender: str) -> str:
        s = (sender or "").lower()
        if s in ("human", "user"):
            return "user"
        if s in ("system", "session", "summary"):
            return "session"
        return "assistant"

    if isinstance(data, dict):
        # Case 1: dict with "conversations" (ShareGPT or oracle_import.json)
        if "conversations" in data and isinstance(data["conversations"], list):
            for msg in data["conversations"]:
                if isinstance(msg, dict):
                    sender = str(msg.get("from") or msg.get("role") or "user")
                    val = _extract_turn_text(msg)
                    if val:
                        role = _determine_role(sender)
                        prefix = f"[{sender.upper()}]: " if sender else ""
                        extracted.append({
                            "sender": sender,
                            "role": role,
                            "content": f"{prefix}{val}" if not val.startswith("[") else val,
                            "raw_text": val,
                        })
                elif isinstance(msg, str) and msg.strip():
                    extracted.append({
                        "sender": "user",
                        "role": "user",
                        "content": msg.strip(),
                        "raw_text": msg.strip(),
                    })
        else:
            # Single object with name/summary or text
            title = data.get("name") or data.get("title") or ""
            summary = data.get("summary") or ""
            if title or summary:
                content = f"[CONVERSATION]: {title}\n[SUMMARY]: {summary}".strip()
                extracted.append({
                    "sender": "system",
                    "role": "session",
                    "content": content,
                    "raw_text": f"{title} - {summary}".strip(),
                })
            val = _extract_turn_text(data)
            if val:
                sender = str(data.get("from") or data.get("role") or data.get("sender") or "user")
                extracted.append({
                    "sender": sender,
                    "role": _determine_role(sender),
                    "content": f"[{sender.upper()}]: {val}",
                    "raw_text": val,
                })
    elif isinstance(data, list):
        for session in data:
            if isinstance(session, dict):
                title = session.get("name") or session.get("title") or ""
                summary = session.get("summary") or ""
                if title or summary:
                    content = f"[CONVERSATION]: {title}\n[SUMMARY]: {summary}".strip()
                    extracted.append({
                        "sender": "system",
                        "role": "session",
                        "content": content,
                        "raw_text": f"{title} - {summary}".strip(),
                    })

                # ShareGPT multi-session
                convs = session.get("conversations")
                if isinstance(convs, list):
                    for msg in convs:
                        if isinstance(msg, dict):
                            sender = str(msg.get("from") or msg.get("role") or "user")
                            val = _extract_turn_text(msg)
                            if val:
                                role = _determine_role(sender)
                                prefix = f"[{sender.upper()}]: " if sender else ""
                                extracted.append({
                                    "sender": sender,
                                    "role": role,
                                    "content": f"{prefix}{val}" if not val.startswith("[") else val,
                                    "raw_text": val,
                                })
                        elif isinstance(msg, str) and msg.strip():
                            extracted.append({
                                "sender": "user",
                                "role": "user",
                                "content": msg.strip(),
                                "raw_text": msg.strip(),
                            })

                # Claude / ChatGPT export
                chat_msgs = session.get("chat_messages")
                if isinstance(chat_msgs, list):
                    for msg in chat_msgs:
                        if isinstance(msg, dict):
                            sender = str(msg.get("sender") or msg.get("from") or msg.get("role") or "user")
                            val = _extract_turn_text(msg)
                            if val:
                                role = _determine_role(sender)
                                prefix = f"[{sender.upper()}]: " if sender else ""
                                extracted.append({
                                    "sender": sender,
                                    "role": role,
                                    "content": f"{prefix}{val}",
                                    "raw_text": val,
                                })
                        elif isinstance(msg, str) and msg.strip():
                            extracted.append({
                                "sender": "user",
                                "role": "user",
                                "content": msg.strip(),
                                "raw_text": msg.strip(),
                            })
                elif not convs and not (title or summary):
                    val = _extract_turn_text(session)
                    if val:
                        sender = str(session.get("from") or session.get("role") or session.get("sender") or "user")
                        extracted.append({
                            "sender": sender,
                            "role": _determine_role(sender),
                            "content": f"[{sender.upper()}]: {val}",
                            "raw_text": val,
                        })
            elif isinstance(session, str) and session.strip():
                extracted.append({
                    "sender": "user",
                    "role": "user",
                    "content": session.strip(),
                    "raw_text": session.strip(),
                })
    return extracted


def parse_json_safely(raw: str) -> list[object]:
    """Parse complete JSON or recover complete JSON objects from a truncated stream."""
    try:
        data = json.loads(raw)
        return [data]
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    s = raw.strip()
    if s.startswith("["):
        s = s[1:].lstrip()
    results: list[object] = []
    idx = 0
    length = len(s)
    while idx < length:
        while idx < length and s[idx] in " \t\r\n,":
            idx += 1
        if idx >= length or s[idx] == "]":
            break
        try:
            obj, end = decoder.raw_decode(s, idx)
            results.append(obj)
            idx = end
        except json.JSONDecodeError:
            break
    return results


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

    # Try parsing as JSON first (supporting truncated streams)
    turns: list[dict[str, str]] = []
    recovered_objs = parse_json_safely(raw_content)
    if recovered_objs:
        for obj in recovered_objs:
            turns.extend(parse_conversations(obj))
        if turns:
            logger.info("Parsed %d structured conversation turns from JSON", len(turns))

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
        import hermes_oracle_memory as _hom
        OracleMemoryProvider = _hom.OracleMemoryProvider
    except ImportError:
        try:
            from __init__ import OracleMemoryProvider
        except (ImportError, Exception):
            init_file = plugin_root / "__init__.py"
            if not init_file.is_file():
                logger.error("Could not locate plugin __init__.py at: %s", init_file)
                return 1
            try:
                spec = importlib.util.spec_from_file_location("hermes_oracle_memory", str(init_file))
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    OracleMemoryProvider = getattr(mod, "OracleMemoryProvider", None)
                    if OracleMemoryProvider is None:
                        logger.error("OracleMemoryProvider class not found in %s", init_file)
                        return 1
                else:
                    logger.error("Failed to create module spec for: %s", init_file)
                    return 1
            except Exception as exc:
                logger.error("Failed to load OracleMemoryProvider from %s: %s", init_file, exc)
                return 1

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
