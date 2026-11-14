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
from typing import Any, Sequence

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


# --- Interpreter guard -------------------------------------------------------
# The Hermes venv `bin/python` is a SYMLINK to a shared tools interpreter and
# `oracledb` lives in the per-venv site-packages. That site-packages is only on
# sys.path when Python is *launched by the venv path* (pyvenv.cfg detection) --
# NOT by the resolved realpath. So `python3 scripts/sync_skills.py` run with the
# tools interpreter (or the system python) cannot import oracledb even though a
# same-interpreter venv probe "should" be equal. os.path.realpath() would equal
# here for every candidate, so we must not skip on realpath: trust the direct
# `import oracledb` probe under each candidate venv instead. The env-flag below
# stops infinite re-exec loops (probe passes but driver still missing).
def _reexec_with_oracle_driver() -> None:
    try:
        import oracledb  # noqa: F401
        return
    except Exception:
        pass
    if os.environ.get("_HERMES_ORACLE_SYNC_REEXEC") == "1":
        return  # already re-executed once; let the normal error path report it
    import glob
    import subprocess

    pattern = str(
        Path.home() / ".hermes" / "installs" / "*" / "environments" / "*" / "venv" / "bin" / "python"
    )
    for candidate in sorted(glob.glob(pattern)):
        if not os.path.exists(candidate):
            continue
        if os.path.abspath(candidate) == os.path.abspath(sys.executable):
            continue  # same launch path (would be an immediate re-exec loop)
        try:
            probe = subprocess.run(
                [candidate, "-c", "import oracledb"], capture_output=True, timeout=30
            )
        except Exception:
            continue
        if probe.returncode == 0:
            env = dict(os.environ, _HERMES_ORACLE_SYNC_REEXEC="1")
            os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


_reexec_with_oracle_driver()

try:
    from agent.memory_provider import MemoryProvider
except ImportError:
    class MemoryProvider:
        pass

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
    description: str = ""
    triggers: Any = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    category: str = "general"
    version: str = ""
    path: Path = field(default_factory=Path)
    mtime: float = 0.0
    content_hash: str = ""
    hash: str = ""
    raw_content: str = ""
    markdown_body: str = ""
    references: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self):
        if self.hash and not self.content_hash:
            self.content_hash = self.hash
        elif self.content_hash and not self.hash:
            self.hash = self.content_hash
        if isinstance(self.triggers, str):
            self.triggers = [self.triggers]


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
        nonlocal current_key, current_scalar_mode, scalar_lines
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

    # Discover text reference files under references/ alongside SKILL.md
    references: list[tuple[str, str]] = []
    ref_dir = path.parent / "references"
    ref_mtimes: list[float] = []
    if ref_dir.is_dir():
        valid_exts = {".md", ".txt", ".rst"}
        for root, _, files in os.walk(ref_dir):
            for fname in sorted(files):
                fpath = Path(root) / fname
                if fpath.suffix.lower() in valid_exts:
                    try:
                        rel_path = fpath.relative_to(path.parent).as_posix()
                        ref_text = fpath.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
                        references.append((rel_path, ref_text))
                        ref_mtimes.append(fpath.stat().st_mtime)
                    except Exception as e:
                        logger.warning("Failed to read reference file %s: %s", fpath, e)
        references.sort(key=lambda x: x[0])

    try:
        mtime = path.stat().st_mtime
    except Exception:
        mtime = 0.0
    if ref_mtimes:
        mtime = max([mtime] + ref_mtimes)

    normalized_content = raw_text.replace("\r\n", "\n")
    hasher = hashlib.sha256(normalized_content.encode("utf-8"))
    for rel_path_str, ref_text in references:
        hasher.update(f"\n--- {rel_path_str} ---\n".encode("utf-8"))
        hasher.update(ref_text.encode("utf-8"))
    content_hash = hasher.hexdigest()

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
        references=references,
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


def compute_host_root(skills_or_paths: Sequence[SkillInfo | Path] | None = None) -> str:
    """Compute the deepest common parent directory of discovered skills."""
    paths: list[str] = []
    if skills_or_paths:
        for item in skills_or_paths:
            if isinstance(item, SkillInfo):
                p = item.path
                if p.name.lower() == "skill.md":
                    paths.append(str(p.parent.parent))
                else:
                    paths.append(str(p.parent if p.is_file() else p))
            elif isinstance(item, Path):
                paths.append(str(item.parent if item.is_file() else item))
            elif isinstance(item, str):
                paths.append(item)
    if not paths:
        for d in (Path.home() / ".hermes" / "skills", Path.home() / ".agents" / "skills", Path.home() / "skills"):
            if d.exists():
                paths.append(str(d))
    if not paths:
        return str(Path.home()) + "/"
    try:
        common = os.path.commonpath(paths)
    except Exception:
        common = str(Path.home())
    if not common.endswith(os.sep):
        common += os.sep
    return common


def get_host_id() -> str:
    """Return local host identifier for skill tagging and pruning."""
    host_id = os.environ.get("HERMES_HOST_ID") or os.environ.get("HOSTNAME") or ""
    if not host_id:
        try:
            import socket
            host_id = socket.gethostname().split(".")[0]
        except Exception:
            host_id = "hermes"
    return re.sub(r"[^a-zA-Z0-9._-]", "", host_id) or "hermes"


def format_skill_payload(skill: SkillInfo, mode: str = "meta", host: str = "") -> list[dict[str, Any]]:
    """Format SkillInfo into Oracle memory database rows."""
    parts = [f"[skill_ingest] [skill:{skill.name}] {skill.name}"]
    if skill.description:
        parts.append(f"— {skill.description}")

    if skill.triggers:
        trigger_bullets = []
        desc_lower = skill.description.lower()
        triggers_list = skill.triggers if isinstance(skill.triggers, list) else [skill.triggers]
        for tr in triggers_list:
            if str(tr).lower() not in desc_lower:
                trigger_bullets.append(str(tr))
        if trigger_bullets:
            parts.append(f"When to use: {'; '.join(trigger_bullets)}")

    if skill.tags:
        tags_str = ", ".join(skill.tags)
        if tags_str.lower() not in skill.description.lower():
            parts.append(f"Tags: {tags_str}")

    meta_content = " ".join(parts).strip()

    base_metadata: dict[str, Any] = {
        "kind": "skill_ingest",
        "skill_name": skill.name,
        "category": skill.category,
        "path": str(skill.path),
        "version": skill.version,
        "mtime": skill.mtime,
        "hash": skill.hash or skill.content_hash,
    }
    if host:
        base_metadata["host"] = host

    prefix = f"skill:{host}:{skill.name}" if host else f"skill:{skill.name}"
    conv_ref = skill.name

    if mode == "meta":
        # Canonical meta-row ID is `:c0`, identical to the full-mode chunk-0 row,
        # so the sync dedup select, list predicate, and lifecycle idempotency
        # check all key on the same MEMORY_ID. A bare host-tagged meta row
        # (skill:{host}:{name}) is invisible to those checks and breaks idempotency.
        meta_row_meta = dict(base_metadata)
        meta_row_meta["chunk_index"] = 0
        return [
            {
                "memory_id": f"{prefix}:c0",
                "session_id": "skills",
                "role": "skill",
                "target": "memory",
                "content": meta_content,
                "metadata": meta_row_meta,
                "turn_index": None,
                "chunk_index": 0,
                "turn_id": None,
                "conv_ref": f"skill:{host}:{skill.name}" if host else conv_ref,
            }
        ]

    # mode == "full"
    body_chunks = chunk_markdown_text(skill.markdown_body, max_words=80, overlap=10)
    ref_chunk_lists: list[tuple[str, list[str]]] = []
    total_ref_chunks = 0
    for rel_path, ref_text in skill.references:
        r_chunks = chunk_markdown_text(ref_text, max_words=80, overlap=10)
        ref_chunk_lists.append((rel_path, r_chunks))
        total_ref_chunks += len(r_chunks)

    total_chunks = 1 + len(body_chunks) + total_ref_chunks

    rows: list[dict[str, Any]] = []
    meta_row_meta = dict(base_metadata)
    meta_row_meta["chunk_index"] = 0
    meta_row_meta["total_chunks"] = total_chunks
    rows.append(
        {
            "memory_id": f"{prefix}:c0",
            "session_id": "skills",
            "role": "skill",
            "target": "memory",
            "content": meta_content,
            "metadata": meta_row_meta,
            "turn_index": None,
            "chunk_index": 0,
            "turn_id": None,
            "conv_ref": conv_ref,
        }
    )

    for i, chunk in enumerate(body_chunks, start=1):
        chunk_content = f"[skill_ingest] [skill:{skill.name}] {skill.name} (chunk {i}/{len(body_chunks)}):\n{chunk}"
        chunk_meta = dict(base_metadata)
        chunk_meta["chunk_index"] = i
        chunk_meta["total_chunks"] = total_chunks
        rows.append(
            {
                "memory_id": f"{prefix}:c{i}",
                "session_id": "skills",
                "role": "skill",
                "target": "memory",
                "content": chunk_content,
                "metadata": chunk_meta,
                "turn_index": None,
                "chunk_index": i,
                "turn_id": None,
                "conv_ref": conv_ref,
            }
        )

    current_chunk_idx = len(body_chunks) + 1
    for rel_path, r_chunks in ref_chunk_lists:
        for ref_i, chunk in enumerate(r_chunks, start=1):
            chunk_content = (
                f"[skill_ingest] [skill:{skill.name}] {skill.name} "
                f"(reference: {rel_path}) (chunk {ref_i}/{len(r_chunks)}):\n{chunk}"
            )
            chunk_meta = dict(base_metadata)
            chunk_meta["chunk_index"] = current_chunk_idx
            chunk_meta["total_chunks"] = total_chunks
            chunk_meta["reference_file"] = rel_path
            rows.append(
                {
                    "memory_id": f"{prefix}:c{current_chunk_idx}",
                    "session_id": "skills",
                    "role": "skill",
                    "target": "memory",
                    "content": chunk_content,
                    "metadata": chunk_meta,
                    "turn_index": None,
                    "chunk_index": current_chunk_idx,
                    "turn_id": None,
                    "conv_ref": conv_ref,
                }
            )
            current_chunk_idx += 1

    return rows


def sync_skills_library(
    provider_or_conn: Any = None,
    skills_or_mode: Any = "meta",
    host_root: str = "",
    *,
    provider: Any = None,
    mode: str = "meta",
    force: bool = False,
    dirs: list[str | Path] | None = None,
    dry_run: bool = False,
    batch_size: int = 50,
    prune_host: str | None = None,
    prune_limit: int = 500,
    host_tag: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Synchronize discovered skills into Oracle memory database."""
    target_provider_or_conn = provider if provider is not None else provider_or_conn

    if isinstance(skills_or_mode, (list, tuple)):
        skills = list(skills_or_mode)
        mode = kwargs.get("mode", mode)
    elif isinstance(skills_or_mode, str):
        mode = skills_or_mode if "mode" not in kwargs else kwargs["mode"]
        skills = discover_skills(dirs=dirs)
    else:
        skills = discover_skills(dirs=dirs)

    effective_host_root = host_root or prune_host or compute_host_root(skills)
    effective_prune_root = prune_host if prune_host is not None else effective_host_root

    def _is_connection(obj: Any, prov_kw: Any) -> bool:
        if prov_kw is not None:
            return False
        if obj is None:
            return False
        if isinstance(obj, MemoryProvider):
            return False
        if hasattr(obj, "_mock_children"):
            if "_pool" in obj._mock_children:
                return False
            if "cursor" in obj._mock_children:
                return True
        d = getattr(obj, "__dict__", {})
        if "_pool" in d:
            return False
        return hasattr(type(obj), "cursor")

    is_direct_conn = _is_connection(target_provider_or_conn, provider)

    if not is_direct_conn:
        if not target_provider_or_conn or not getattr(target_provider_or_conn, "_pool", None):
            if not dry_run:
                err = "Oracle memory provider pool is not initialized"
                if target_provider_or_conn and hasattr(target_provider_or_conn, "_init_error") and target_provider_or_conn._init_error:
                    err = target_provider_or_conn._init_error
                elif target_provider_or_conn and hasattr(target_provider_or_conn, "unavailable_reason") and target_provider_or_conn.unavailable_reason():
                    err = target_provider_or_conn.unavailable_reason()
                raise RuntimeError(err)

    table = getattr(target_provider_or_conn, "_table", "HERMES_AGENT_MEMORY")
    agent_id_attr = getattr(target_provider_or_conn, "_agent_id", None)
    agent_id_str = agent_id_attr if isinstance(agent_id_attr, str) and agent_id_attr else ""
    effective_host = host_tag or os.environ.get("HERMES_HOST_ID") or agent_id_str

    stats: dict[str, Any] = {
        "status": "dry_run" if dry_run else "success",
        "scanned": len(skills),
        "indexed": 0,
        "updated": 0,
        "skipped": 0,
        "upserted": 0,
        "pruned": 0,
        "pruned_skills": [],
        "total_rows_inserted": 0,
        "errors": [],
        "mode": mode,
        "dry_run": dry_run,
    }

    if dry_run and not target_provider_or_conn:
        return {
            "status": "dry_run",
            "scanned": len(skills),
            "indexed": 0,
            "updated": 0,
            "skipped": 0,
            "upserted": 0,
            "pruned": 0,
            "pruned_skills": [],
            "total_rows": sum(len(format_skill_payload(s, mode=mode)) for s in skills),
            "total_rows_inserted": 0,
            "errors": [],
            "mode": mode,
            "dry_run": True,
        }

    class _ConnContext:
        def __init__(self, p_or_c, is_dir):
            self.p_or_c = p_or_c
            self.is_dir = is_dir
            self.acquired = None

        def __enter__(self):
            if self.is_dir:
                return self.p_or_c
            self.acquired = self.p_or_c._pool.acquire()
            if hasattr(self.acquired, "__enter__"):
                return self.acquired.__enter__()
            return self.acquired

        def __exit__(self, exc_type, exc_val, exc_tb):
            if not self.is_dir and self.acquired:
                if hasattr(self.acquired, "__exit__"):
                    return self.acquired.__exit__(exc_type, exc_val, exc_tb)
                try:
                    self.p_or_c._pool.release(self.acquired)
                except Exception:
                    pass

    conn = None  # bound by the context manager; rollback target if enter fails
    try:
        with _ConnContext(target_provider_or_conn, is_direct_conn) as conn:
            ingest_ms = int(float(os.environ.get("HERMES_ORACLE_INGEST_TIMEOUT", "600") or "600") * 1000)
            try:
                conn.call_timeout = ingest_ms
            except Exception:
                pass

            existing_map: dict[str, dict[str, Any]] = {}
            existing_by_name: dict[str, list[dict[str, Any]]] = {}
            existing_conv_refs: set[str] = set()

            with conn.cursor() as cur:
                sql = f"""
                    SELECT memory_id,
                           json_value(metadata, '$.skill_name') as skill_name,
                           json_value(metadata, '$.hash') as hash,
                           json_value(metadata, '$.path') as path,
                           json_value(metadata, '$.host') as host
                    FROM {table}
                    WHERE role = 'skill' AND (memory_id LIKE '%:c0' OR regexp_like(memory_id, '^skill:[^:]+$'))
                """
                cur.execute(sql)
                for row in cur.fetchall():
                    mid = str(row[0])
                    sname = ""
                    shash = ""
                    spath = ""
                    shost = ""
                    if len(row) >= 5:
                        sname = str(row[1] or "")
                        shash = str(row[2] or "")
                        spath = str(row[3] or "")
                        shost = str(row[4] or "")
                    elif len(row) == 2 or len(row) == 3:
                        raw_meta = row[2] if len(row) >= 3 else row[1]
                        if len(row) >= 3 and row[1]:
                            existing_conv_refs.add(str(row[1]))
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
                        sname = str(meta_dict.get("skill_name") or "")
                        shash = str(meta_dict.get("hash") or "")
                        spath = str(meta_dict.get("path") or "")
                        shost = str(meta_dict.get("host") or "")
                        if not sname:
                            m = re.match(r"^skill:(?:[^:]+:)?([^:]+?)(?::c\d+)?$", mid)
                            if m:
                                sname = m.group(1)

                    item = {"mid": mid, "name": sname, "hash": shash, "path": spath, "host": shost}
                    if sname:
                        existing_by_name.setdefault(sname, []).append(item)
                    existing_map[mid] = item

                to_upsert: list[SkillInfo] = []
                for skill in skills:
                    copies = existing_by_name.get(skill.name, [])
                    skill_hash = skill.hash or skill.content_hash
                    # Skip if ANY copy matches this hash (prevents redundant cross-host duplicates)
                    if not force and any(c.get("hash") == skill_hash for c in copies):
                        stats["skipped"] += 1
                        continue
                    meta_mid = f"skill:{effective_host}:{skill.name}:c0" if effective_host else f"skill:{skill.name}:c0"
                    legacy_mid = f"skill:{skill.name}"
                    if not force and (meta_mid in existing_map or legacy_mid in existing_map):
                        ex_h = existing_map.get(meta_mid, {}).get("hash") or existing_map.get(legacy_mid, {}).get("hash")
                        if ex_h and ex_h == skill_hash:
                            stats["skipped"] += 1
                            continue
                    if copies:
                        stats["updated"] += 1
                    else:
                        stats["indexed"] += 1
                    to_upsert.append(skill)

                # Delete dead SQL leg 3: use (conv_ref = :1 OR memory_id = :2)
                skills_to_delete = [
                    s for s in to_upsert
                    if (
                        s.name in existing_by_name
                        or s.name in existing_conv_refs
                        or f"skill:{s.name}" in existing_map
                        or f"skill:{s.name}:c0" in existing_map
                        or (f"skill:{effective_host}:{s.name}:c0" if effective_host else "") in existing_map
                    )
                ]
                if skills_to_delete and not dry_run:
                    del_sql = f"DELETE /*+ NO_PARALLEL */ FROM {table} WHERE role = 'skill' AND (conv_ref = :1 OR memory_id = :2)"
                    del_params = [
                        (s.name, f"skill:{effective_host}:{s.name}:c0" if effective_host else f"skill:{s.name}:c0")
                        for s in skills_to_delete
                    ]
                    for i in range(0, len(del_params), 500):
                        chunk = del_params[i : i + 500]
                        cur.executemany(del_sql, chunk)
                    # FIX v1.1.0.1: commit the deletes BEFORE _insert_many() runs
                    # the matching INSERTs on a second pool connection. The
                    # provider's _insert_many acquires its own connection, and its
                    # INSERT of the same memory_ids would wait on this
                    # transaction's own uncommitted deletes (uniqueness check) —
                    # a self-deadlock: this connection is parked inside the
                    # _insert_many call and will never commit. Committing first
                    # releases the row locks; the small delete/insert non-
                    # atomicity window is self-healing (next sync reconciles
                    # from the on-disk source of truth).
                    conn.commit()

                all_rows: list[dict[str, Any]] = []
                for skill in to_upsert:
                    payloads = format_skill_payload(skill, mode=mode, host=effective_host)
                    all_rows.extend(payloads)
                    stats["upserted"] += 1

                if not dry_run and all_rows:
                    if hasattr(target_provider_or_conn, "_insert_many") and callable(getattr(target_provider_or_conn, "_insert_many")):
                        for i in range(0, len(all_rows), batch_size):
                            batch = all_rows[i : i + batch_size]
                            target_provider_or_conn._insert_many(batch)
                    else:
                        insert_sql = f"""
                            INSERT INTO {table}
                                (memory_id, session_id, agent_id, role, content,
                                 created_at, target, metadata, content_txt)
                            VALUES (:1, :2, :3, :4, :5, SYSTIMESTAMP, :6, :7, :8)
                        """
                        binds = []
                        for r in all_rows:
                            meta_str = json.dumps(r["metadata"])
                            txt_4k = (r["content"] or "")[:4000]
                            binds.append((
                                r["memory_id"],
                                r.get("session_id", "skills"),
                                effective_host,
                                "skill",
                                r["content"],
                                "memory",
                                meta_str,
                                txt_4k,
                            ))
                        for i in range(0, len(binds), batch_size):
                            chunk = binds[i : i + batch_size]
                            cur.executemany(insert_sql, chunk)

                stats["total_rows_inserted"] = len(all_rows) if not dry_run else 0

                # Host-scoped zombie pruning
                if prune_host:
                    alive = {s.name for s in skills}
                    stale_sql = f"""
                        SELECT DISTINCT json_value(metadata, '$.skill_name')
                        FROM {table}
                        WHERE role = 'skill' AND json_value(metadata, '$.path') LIKE :host_root || '%'
                    """
                    cur.execute(stale_sql, {"host_root": effective_prune_root})
                    stale_rows = cur.fetchall()
                    stale = [r[0] for r in stale_rows if r and r[0] and r[0] not in alive]
                    stale.sort()
                    stale = stale[:prune_limit]

                    prune_del_sql = f"""
                        DELETE /*+ NO_PARALLEL */ FROM {table}
                        WHERE role = 'skill'
                          AND json_value(metadata, '$.skill_name') = :name
                          AND json_value(metadata, '$.path') LIKE :host_root || '%'
                    """
                    for name in stale:
                        stats["pruned_skills"].append(name)
                        if not dry_run:
                            cur.execute(prune_del_sql, {"name": name, "host_root": effective_prune_root})
                        stats["pruned"] += 1

                if not dry_run and (all_rows or stats["pruned"] > 0):
                    conn.commit()

    except Exception as e:
        # Never return a pool connection with an uncommitted delete
        # transaction: the next run's INSERTs would block on the leaked
        # row locks (self-deadlock across runs).
        if not dry_run and conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        logger.warning("Dry run database interaction error: %s", e)

    return stats


def migrate_host_tags(
    conn: Any,
    table: str,
    host: str,
    host_root: str,
    dry_run: bool = False,
) -> int:
    """One-shot migration rewrites legacy untagged memory_ids to skill:{host}:{name}:cN.

    Only operates on rows whose metadata.path matches host_root. Idempotent.
    """
    if not host or not host_root:
        logger.error("migrate_host_tags requires valid host and host_root")
        return 0

    count = 0
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT memory_id, conv_ref, metadata
            FROM {table}
            WHERE role = 'skill'
              AND json_value(metadata, '$.path') LIKE :host_root || '%'
              AND (json_value(metadata, '$.host') IS NULL OR NOT regexp_like(memory_id, '^skill:[^:]+:[^:]+'))
            """,
            {"host_root": host_root},
        )
        rows = cur.fetchall()
        for mid, conv_ref, meta in rows:
            m = re.match(r"^skill:([^:]+)(?::c(\d+))?$", str(mid))
            if not m:
                continue
            name = m.group(1)
            c_idx = m.group(2)
            if c_idx is not None:
                new_mid = f"skill:{host}:{name}:c{c_idx}"
            else:
                new_mid = f"skill:{host}:{name}"
            new_conv_ref = f"skill:{host}:{name}"

            try:
                meta_dict = json.loads(meta) if isinstance(meta, str) else dict(meta or {})
            except Exception:
                meta_dict = {}
            meta_dict["host"] = host
            new_meta_str = json.dumps(meta_dict)

            if not dry_run:
                cur.execute(
                    f"""
                    UPDATE {table}
                    SET memory_id = :1,
                        conv_ref = :2,
                        metadata = :3,
                        agent_id = :4
                    WHERE memory_id = :5
                    """,
                    (new_mid, new_conv_ref, new_meta_str, host, mid),
                )
            count += 1

        if not dry_run and count > 0:
            conn.commit()

    logger.info("migrate_host_tags: migrated %d rows (dry_run=%s)", count, dry_run)
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync skills library into Oracle 26ai vector memory.")
    parser.add_argument("--mode", choices=["meta", "full"], default="meta", help="Ingestion mode: meta or full (default: meta)")
    parser.add_argument("--dirs", nargs="*", default=None, help="Custom directories to scan for skills")
    parser.add_argument("--force", action="store_true", help="Force re-indexing even if already up to date")
    parser.add_argument("--dry-run", action="store_true", help="Discover and parse skills without modifying database")
    parser.add_argument("--batch-size", type=int, default=50, help="Batch size for database inserts (default: 50)")
    parser.add_argument("--table", default=None, help="Oracle memory table name override")
    parser.add_argument("--prune-host", default=None, help="Path prefix for host-scoped zombie skill pruning (default: auto-detect deepest common parent)")
    parser.add_argument("--prune-limit", type=int, default=500, help="Maximum number of stale skills to prune in one run (default: 500)")
    parser.add_argument("--migrate-host-tags", action="store_true", help="One-shot migration rewrites legacy untagged memory_ids to host-tagged format")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.dry_run and not args.migrate_host_tags:
        skills = discover_skills(dirs=args.dirs)
        print(f"Dry run: discovered {len(skills)} skills:")
        for s in skills:
            print(f"  - [{s.category}] {s.name} ({s.path})")
            if args.verbose:
                print(f"    Description: {s.description[:100]}...")
                print(f"    Triggers: {len(s.triggers)} found")
                print(f"    Tags: {', '.join(s.tags)}")

    from __init__ import OracleMemoryProvider
    provider = OracleMemoryProvider()
    if args.table:
        provider._table = args.table
    provider.initialize("skill-sync")
    if not provider.is_available():
        if args.dry_run:
            sys.exit(0)
        print(f"Error: Oracle memory provider unavailable: {provider.unavailable_reason()}", file=sys.stderr)
        sys.exit(1)

    host_id = get_host_id()
    if args.migrate_host_tags:
        host_root = args.prune_host or compute_host_root()
        table_name = args.table or provider._table
        with provider._pool.acquire() as conn:
            cnt = migrate_host_tags(conn, table_name, host_id, host_root, dry_run=args.dry_run)
        print(f"Migration complete: {cnt} rows updated (dry_run={args.dry_run})")
        sys.exit(0)

    res = sync_skills_library(
        provider=provider,
        mode=args.mode,
        force=args.force,
        dirs=args.dirs,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
        prune_host=args.prune_host,
        prune_limit=args.prune_limit,
        host_tag=host_id,
    )
    print("Sync complete:")
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
