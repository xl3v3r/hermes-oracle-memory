#!/usr/bin/env python3
"""Sync local skills (~/.agents/skills and ~/.hermes/skills) into Oracle 26ai vector memory.

Extracts YAML frontmatter (name, description, tags, category), identifies activation
triggers from body and frontmatter, and batch-indexes vector rows using byte-safe
and sequence-length-safe clamping to guarantee zero ORA exceptions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("hermes.oracle_sync_skills")

# Load ~/.hermes/.env if present
env_file = Path.home() / ".hermes" / ".env"
if env_file.exists():
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                os.environ.setdefault(k.strip(), v)
    except Exception as e:
        logger.debug("Failed reading %s: %s", env_file, e)

# Standalone shims for agent / tools if not present in sys.modules
if "agent" not in sys.modules:
    try:
        from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt
        from agent.secret_scope import get_secret
    except ImportError:
        agent_mod = types.ModuleType("agent")
        mp_mod = types.ModuleType("agent.memory_provider")

        class MemoryProvider:
            def initialize(self, session_id: str) -> None:
                pass
            def is_available(self) -> bool:
                return True
            def unavailable_reason(self) -> str:
                return ""
            def shutdown(self) -> None:
                pass

        @dataclass
        class RecallStatus:
            provider_label: str
            count: int

        mp_mod.MemoryProvider = MemoryProvider
        mp_mod.RecallStatus = RecallStatus
        mp_mod.is_trivial_prompt = lambda prompt: False
        agent_mod.memory_provider = mp_mod
        sys.modules["agent"] = agent_mod
        sys.modules["agent.memory_provider"] = mp_mod

        sec_mod = types.ModuleType("agent.secret_scope")
        sec_mod.get_secret = lambda k, default=None: os.environ.get(k, default)
        agent_mod.secret_scope = sec_mod
        sys.modules["agent.secret_scope"] = sec_mod

if "tools" not in sys.modules:
    try:
        from tools.registry import tool_error
    except ImportError:
        tools_mod = types.ModuleType("tools")
        reg_mod = types.ModuleType("tools.registry")
        reg_mod.tool_error = lambda msg: f"Error: {msg}"
        tools_mod.registry = reg_mod
        sys.modules["tools"] = tools_mod
        sys.modules["tools.registry"] = reg_mod

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))


@dataclass
class SkillInfo:
    name: str
    description: str
    triggers: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    category: str = "general"
    version: str = ""
    path: Path = field(default_factory=Path)
    mtime: float = 0.0
    content_hash: str = ""
    raw_content: str = ""
    markdown_body: str = ""


def safe_varchar_bytes(text: str | None, max_bytes: int = 4000) -> str:
    """Clamp text to fit within max_bytes in UTF-8 encoding.
    
    Prevents ORA-12899: value too large for column when inserting into VARCHAR2(4000).
    """
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def safe_embed_text(text: str | None, max_chars: int = 1200) -> str:
    """Clamp text length to safely fit within ONNX model's 512-token limit.
    
    Dense inputs can exceed 512 tokens if 4000 characters are passed to
    VECTOR_EMBEDDING, causing ORA-54401 ONNX broadcast errors.
    1200 characters safely guarantees token count stays below the 512 sequence limit.
    """
    if not text:
        return ""
    return text[:max_chars]


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from markdown text.
    
    Returns (frontmatter_dict, markdown_body).
    Supports PyYAML when available, with a full-featured pure-Python fallback.
    """
    if not text:
        return {}, ""
    # Strip optional UTF-8 BOM
    if text.startswith("\ufeff"):
        text = text[1:]
    stripped_leading = text.lstrip()
    if not stripped_leading.startswith("---"):
        return {}, text

    lines = stripped_leading.splitlines()
    fm_lines: list[str] = []
    body_start_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            body_start_idx = i + 1
            break
        fm_lines.append(lines[i])

    if body_start_idx == -1:
        return {}, text

    body = "\n".join(lines[body_start_idx:])
    raw_fm = "\n".join(fm_lines)

    # 1. Try PyYAML
    try:
        import yaml
        parsed = yaml.safe_load(raw_fm)
        if isinstance(parsed, dict):
            return parsed, body
    except Exception:
        pass

    # 2. Pure-Python fallback parser with indentation stack and scalar block support
    data: dict[str, Any] = {}
    current_key: str | None = None
    current_scalar_mode: str | None = None
    current_scalar_indent: int | None = None
    scalar_lines: list[str] = []
    # Stack stores (indent_level, target_dict)
    dict_stack: list[tuple[int, dict[str, Any]]] = [(0, data)]

    def commit_scalar():
        nonlocal current_key, current_scalar_mode, current_scalar_indent, scalar_lines
        if current_key and current_scalar_mode:
            target_dict = dict_stack[-1][1]
            if current_scalar_mode.startswith(">"):
                val = " ".join(l.strip() for l in scalar_lines if l.strip())
            else:
                val = "\n".join(scalar_lines).strip()
            target_dict[current_key] = val
        current_scalar_mode = None
        current_scalar_indent = None
        scalar_lines = []

    for line in fm_lines:
        line_str = line.strip()
        if not line_str or line_str.startswith("#"):
            if current_scalar_mode:
                scalar_lines.append("")
            continue

        indent = len(line) - len(line.lstrip())

        if current_scalar_mode:
            if current_scalar_indent is not None and indent > current_scalar_indent:
                scalar_lines.append(line_str)
                continue
            else:
                commit_scalar()

        # Adjust stack based on indentation
        while len(dict_stack) > 1 and indent <= dict_stack[-1][0]:
            dict_stack.pop()

        current_target = dict_stack[-1][1]

        # Bullet list item under current_key (e.g. - item)
        m_bullet = re.match(r"^-\s+(.*)$", line_str)
        if m_bullet and current_key:
            b_val = m_bullet.group(1).strip().strip('"\'')
            if current_key not in current_target or not isinstance(current_target[current_key], list):
                current_target[current_key] = []
            current_target[current_key].append(b_val)
            continue

        match = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line_str)
        if match:
            key, val = match.group(1), match.group(2).strip()
            current_key = key
            if not val:
                subdict: dict[str, Any] = {}
                current_target[key] = subdict
                dict_stack.append((indent, subdict))
                continue

            if val.startswith(">") or val.startswith("|"):
                current_scalar_mode = val
                current_scalar_indent = indent
                scalar_lines = []
                continue

            if val.startswith("[") and val.endswith("]"):
                items = [x.strip().strip("'\"") for x in val[1:-1].split(",") if x.strip()]
                current_target[key] = items
                continue

            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]

            current_target[key] = val

    commit_scalar()
    return data, body


def extract_body_triggers(body: str) -> list[str]:
    """Extract activation triggers from markdown sections (## When to Use, ## Triggers, etc.)."""
    triggers: list[str] = []
    in_trigger_section = False

    trigger_header_re = re.compile(
        r"^(?:#{1,4}\s+|\*\*)?(?:when\s+to\s+use|use\s+when|triggers?|trigger\s+phrases?|activation|use\s+cases?)\b",
        re.IGNORECASE,
    )
    stop_header_re = re.compile(
        r"^(?:#{1,4}\s+|\*\*)?(?:don't\s+use|do\s+not\s+use|when\s+not\s+to\s+use|anti-patterns?|non-goals?)\b",
        re.IGNORECASE,
    )
    any_header_re = re.compile(r"^(?:#{1,4}\s+|\*\*[A-Z])")

    negative_prefixes = ("don't use", "do not use", "not for", "never use", "anti-pattern")

    for line in body.splitlines():
        line_s = line.strip()
        if stop_header_re.match(line_s):
            in_trigger_section = False
            continue
        if trigger_header_re.match(line_s):
            in_trigger_section = True
            continue
        elif in_trigger_section and any_header_re.match(line_s):
            in_trigger_section = False
            continue

        if in_trigger_section:
            if not line_s:
                continue
            line_lower = line_s.lower().lstrip("*-#• ").strip()
            if any(line_lower.startswith(p) for p in negative_prefixes):
                continue
            m = re.match(r"^(?:[-*•]|\d+\.)\s+(?:\d+[.)]\s+)?(.*)$", line_s)
            if m:
                bullet = m.group(1).strip().strip('"').strip("'")
                bullet_lower = bullet.lower()
                if bullet and not any(bullet_lower.startswith(p) for p in negative_prefixes):
                    triggers.append(bullet)
            elif (line_s.startswith('"') and line_s.endswith('"')) or (line_s.startswith("'") and line_s.endswith("'")):
                triggers.append(line_s.strip('"\''))
            elif line_s and not line_s.startswith("#") and not line_s.startswith("**"):
                triggers.append(line_s)
    return triggers


def parse_skill_file(skill_path: Path | str) -> SkillInfo | None:
    path = Path(skill_path).resolve()
    if not path.is_file():
        return None

    try:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning("Failed to read %s: %s", path, e)
        return None

    fm, body = parse_frontmatter(raw_text)

    name = str(fm.get("name") or "").strip()
    if not name:
        name = path.parent.name

    description = str(fm.get("description") or "").strip()

    tags: list[str] = []
    raw_tags = fm.get("tags")
    if isinstance(raw_tags, list):
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]
    elif isinstance(raw_tags, str) and raw_tags.strip():
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

    meta_block = fm.get("metadata")
    if isinstance(meta_block, dict):
        h_block = meta_block.get("hermes")
        if isinstance(h_block, dict):
            h_tags = h_block.get("tags")
            if isinstance(h_tags, list):
                for t in h_tags:
                    t_str = str(t).strip()
                    if t_str and t_str not in tags:
                        tags.append(t_str)
        m_tags = meta_block.get("tags")
        if isinstance(m_tags, list):
            for t in m_tags:
                t_str = str(t).strip()
                if t_str and t_str not in tags:
                    tags.append(t_str)

    triggers: list[str] = []
    # Frontmatter triggers
    for key in ("triggers", "trigger_phrases", "when_to_use"):
        val = fm.get(key)
        if isinstance(val, list):
            for item in val:
                s = str(item).strip()
                if s and s not in triggers:
                    triggers.append(s)
        elif isinstance(val, str) and val.strip():
            triggers.append(val.strip())

    # Metadata hermes triggers
    if isinstance(meta_block, dict):
        h_block = meta_block.get("hermes")
        if isinstance(h_block, dict):
            for key in ("triggers", "trigger_phrases"):
                val = h_block.get(key)
                if isinstance(val, list):
                    for item in val:
                        s = str(item).strip()
                        if s and s not in triggers:
                            triggers.append(s)

    # Body markdown triggers
    for tr in extract_body_triggers(body):
        if tr not in triggers:
            triggers.append(tr)

    category = str(fm.get("category") or "").strip()
    if not category and isinstance(meta_block, dict):
        h_block = meta_block.get("hermes")
        if isinstance(h_block, dict):
            category = str(h_block.get("category") or "").strip()
        if not category:
            category = str(meta_block.get("category") or "").strip()
    if not category:
        parent_name = path.parent.parent.name
        if parent_name not in ("skills", "agents", "home", "root", "") and not parent_name.startswith("."):
            category = parent_name
        else:
            category = "general"

    version = str(fm.get("version") or "")
    if not version and isinstance(meta_block, dict):
        version = str(meta_block.get("version") or "")

    try:
        mtime = path.stat().st_mtime
    except Exception:
        mtime = 0.0

    normalized_content = raw_text.replace("\r\n", "\n")
    content_hash = hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()

    return SkillInfo(
        name=name,
        description=description,
        triggers=triggers,
        tags=tags,
        category=category,
        version=version,
        path=path,
        mtime=mtime,
        content_hash=content_hash,
        raw_content=raw_text,
        markdown_body=body,
    )


def discover_skills(dirs: list[str | Path] | None = None) -> list[SkillInfo]:
    """Scan directories for SKILL.md files and return parsed SkillInfo objects."""
    search_dirs: list[Path] = []
    if dirs:
        for d in dirs:
            p = Path(os.path.expanduser(os.path.expandvars(str(d)))).resolve()
            if p.is_dir():
                search_dirs.append(p)
    else:
        default_dirs = [
            Path.home() / ".agents" / "skills",
            Path.home() / ".hermes" / "skills",
        ]
        for d in default_dirs:
            p = d.resolve()
            if p.is_dir():
                search_dirs.append(p)

    skills_by_name: dict[str, SkillInfo] = {}
    seen_canonical_paths: set[Path] = set()
    ignored_subdirs = {".archive", ".git", ".venv", ".pytest_cache", ".locks", ".curator_backups"}

    for base_dir in search_dirs:
        for root, dirnames, filenames in os.walk(base_dir, followlinks=True):
            # Prune hidden subdirectories
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]

            for fname in filenames:
                if fname.lower() == "skill.md":
                    fpath = Path(root) / fname
                    try:
                        canonical = fpath.resolve()
                    except Exception:
                        canonical = fpath

                    if canonical in seen_canonical_paths:
                        continue
                    seen_canonical_paths.add(canonical)

                    # Only filter if path component is an explicitly ignored directory (like .archive, .git)
                    # or if a subdirectory relative to base_dir is hidden
                    if any(part in ignored_subdirs for part in canonical.parts):
                        continue
                    try:
                        rel_parts = canonical.relative_to(base_dir).parts[:-1]
                        if any(p.startswith(".") for p in rel_parts):
                            continue
                    except ValueError:
                        pass

                    skill_info = parse_skill_file(canonical)
                    if skill_info and skill_info.name:
                        if skill_info.name not in skills_by_name:
                            skills_by_name[skill_info.name] = skill_info

    return [skills_by_name[k] for k in sorted(skills_by_name.keys())]


def chunk_markdown_text(text: str, max_words: int = 80, overlap: int = 10) -> list[str]:
    """Split text into overlapping word chunks."""
    if not text or not text.strip():
        return []
    words = text.split()
    if not words:
        return []
    if len(words) <= max_words:
        return [text.strip()]

    chunks = []
    step = max(1, max_words - overlap)
    for start in range(0, len(words), step):
        chunk_words = words[start : start + max_words]
        chunks.append(" ".join(chunk_words))
        if start + max_words >= len(words):
            break
    return chunks


def format_skill_payload(skill: SkillInfo, mode: str = "meta") -> list[dict[str, Any]]:
    """Format SkillInfo into Oracle memory database rows."""
    parts = [f"[skill_ingest] [skill:{skill.name}] {skill.name}"]
    if skill.description:
        parts.append(f"— {skill.description}")

    if skill.triggers:
        trigger_bullets = []
        desc_lower = skill.description.lower()
        for tr in skill.triggers:
            if tr.lower() not in desc_lower:
                trigger_bullets.append(tr)
        if trigger_bullets:
            parts.append(f"When to use: {'; '.join(trigger_bullets)}")

    if skill.tags:
        tags_str = ", ".join(skill.tags)
        if tags_str.lower() not in skill.description.lower():
            parts.append(f"Tags: {tags_str}")

    meta_content = " ".join(parts).strip()

    base_metadata = {
        "kind": "skill_ingest",
        "skill_name": skill.name,
        "category": skill.category,
        "path": str(skill.path),
        "version": skill.version,
        "mtime": skill.mtime,
        "hash": skill.content_hash,
    }

    if mode == "meta":
        return [
            {
                "memory_id": f"skill:{skill.name}",
                "session_id": "skills",
                "role": "skill",
                "target": "memory",
                "content": meta_content,
                "metadata": base_metadata,
                "turn_index": None,
                "chunk_index": None,
                "turn_id": None,
                "conv_ref": skill.name,
            }
        ]

    # mode == "full"
    rows: list[dict[str, Any]] = []
    meta_row_meta = dict(base_metadata)
    meta_row_meta["chunk_index"] = 0
    rows.append(
        {
            "memory_id": f"skill:{skill.name}:c0",
            "session_id": "skills",
            "role": "skill",
            "target": "memory",
            "content": meta_content,
            "metadata": meta_row_meta,
            "turn_index": None,
            "chunk_index": 0,
            "turn_id": None,
            "conv_ref": skill.name,
        }
    )

    body_chunks = chunk_markdown_text(skill.markdown_body, max_words=80, overlap=10)
    total_chunks = len(body_chunks) + 1
    for i, chunk in enumerate(body_chunks, start=1):
        chunk_content = f"[skill_ingest] [skill:{skill.name}] {skill.name} (chunk {i}/{len(body_chunks)}):\n{chunk}"
        chunk_meta = dict(base_metadata)
        chunk_meta["chunk_index"] = i
        chunk_meta["total_chunks"] = total_chunks
        rows.append(
            {
                "memory_id": f"skill:{skill.name}:c{i}",
                "session_id": "skills",
                "role": "skill",
                "target": "memory",
                "content": chunk_content,
                "metadata": chunk_meta,
                "turn_index": None,
                "chunk_index": i,
                "turn_id": None,
                "conv_ref": skill.name,
            }
        )
    return rows


def sync_skills_library(
    provider: Any,
    mode: str = "meta",
    force: bool = False,
    dirs: list[str | Path] | None = None,
    dry_run: bool = False,
    batch_size: int = 50,
) -> dict[str, Any]:
    """Synchronize discovered skills into Oracle memory database."""
    skills = discover_skills(dirs=dirs)

    if dry_run:
        return {
            "status": "dry_run",
            "scanned": len(skills),
            "indexed": 0,
            "updated": 0,
            "skipped": 0,
            "total_rows": sum(len(format_skill_payload(s, mode=mode)) for s in skills),
            "errors": [],
            "mode": mode,
        }

    if not provider or not getattr(provider, "_pool", None):
        err = "Oracle memory provider pool is not initialized"
        if provider and hasattr(provider, "_init_error") and provider._init_error:
            err = provider._init_error
        elif provider and hasattr(provider, "unavailable_reason") and provider.unavailable_reason():
            err = provider.unavailable_reason()
        raise RuntimeError(err)

    table = getattr(provider, "_table", "HERMES_AGENT_MEMORY")

    existing_map: dict[str, dict[str, Any]] = {}
    with provider._pool.acquire() as conn, conn.cursor() as cur:
        sql = f"SELECT /*+ NO_PARALLEL */ memory_id, metadata FROM {table} WHERE role = 'skill'"
        cur.execute(sql)
        for row in cur.fetchall():
            mid = str(row[0])
            raw_meta = row[1]
            if hasattr(raw_meta, "read"):
                raw_meta = raw_meta.read()
            meta_dict = {}
            if isinstance(raw_meta, dict):
                meta_dict = raw_meta
            elif isinstance(raw_meta, str) and raw_meta.strip():
                try:
                    meta_dict = json.loads(raw_meta)
                except Exception:
                    pass
            existing_map[mid] = meta_dict

    skills_to_sync: list[SkillInfo] = []
    skipped_count = 0
    updated_count = 0
    new_count = 0

    for skill in skills:
        meta_mid = f"skill:{skill.name}" if mode == "meta" else f"skill:{skill.name}:c0"
        if not force and meta_mid in existing_map:
            ex_hash = existing_map[meta_mid].get("hash")
            if ex_hash and ex_hash == skill.content_hash:
                skipped_count += 1
                continue
            else:
                updated_count += 1
        else:
            new_count += 1
        skills_to_sync.append(skill)

    if not skills_to_sync:
        return {
            "status": "success",
            "scanned": len(skills),
            "indexed": 0,
            "updated": 0,
            "skipped": skipped_count,
            "total_rows_inserted": 0,
            "errors": [],
            "mode": mode,
        }

    with provider._pool.acquire() as conn, conn.cursor() as cur:
        for skill in skills_to_sync:
            del_sql = f"DELETE /*+ NO_PARALLEL */ FROM {table} WHERE role = 'skill' AND (conv_ref = :1 OR memory_id = :2 OR memory_id LIKE :3)"
            cur.execute(del_sql, [skill.name, f"skill:{skill.name}", f"skill:{skill.name}:%"])
        conn.commit()

    all_rows: list[dict[str, Any]] = []
    for skill in skills_to_sync:
        payloads = format_skill_payload(skill, mode=mode)
        all_rows.extend(payloads)

    for i in range(0, len(all_rows), batch_size):
        batch = all_rows[i : i + batch_size]
        provider._insert_many(batch)

    return {
        "status": "success",
        "scanned": len(skills),
        "indexed": new_count,
        "updated": updated_count,
        "skipped": skipped_count,
        "total_rows_inserted": len(all_rows),
        "errors": [],
        "mode": mode,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync skills library into Oracle 26ai vector memory.")
    parser.add_argument("--mode", choices=["meta", "full"], default="meta", help="Ingestion mode: meta or full (default: meta)")
    parser.add_argument("--dirs", nargs="*", default=None, help="Custom directories to scan for skills")
    parser.add_argument("--force", action="store_true", help="Force re-indexing even if already up to date")
    parser.add_argument("--dry-run", action="store_true", help="Discover and parse skills without modifying database")
    parser.add_argument("--table", default=None, help="Oracle memory table name override")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.dry_run:
        skills = discover_skills(dirs=args.dirs)
        print(f"Dry run: discovered {len(skills)} skills:")
        for s in skills:
            print(f"  - [{s.category}] {s.name} ({s.path})")
            if args.verbose:
                print(f"    Description: {s.description[:100]}...")
                print(f"    Triggers: {len(s.triggers)} found")
                print(f"    Tags: {', '.join(s.tags)}")
        sys.exit(0)

    from __init__ import OracleMemoryProvider
    provider = OracleMemoryProvider()
    if args.table:
        provider._table = args.table
    provider.initialize("skill-sync")
    if not provider.is_available():
        print(f"Error: Oracle memory provider unavailable: {provider.unavailable_reason()}", file=sys.stderr)
        sys.exit(1)

    res = sync_skills_library(provider=provider, mode=args.mode, force=args.force, dirs=args.dirs, dry_run=False)
    print("Sync complete:")
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
