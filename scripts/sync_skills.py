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



def load_env_file(env_file: Path) -> None:
    """CLI --env-file: set KEY=VALUE lines that are not already in the
    environment. The CLI never reads a credentials file on its own (1.1.1.4):
    export OCI_DB_* or pass --env-file explicitly."""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


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


if __name__ == "__main__":  # never re-exec a host process that imported us
    _reexec_with_oracle_driver()

try:
    from agent.memory_provider import MemoryProvider
except ImportError:
    class MemoryProvider:
        pass

# Standalone shims for agent / tools if not present in sys.modules
if "agent" not in sys.modules:
    try:
        from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt  # noqa: F401
        from agent.secret_scope import get_secret  # noqa: F401
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
        from tools.registry import tool_error  # noqa: F401
    except ImportError:
        tools_mod = types.ModuleType("tools")
        reg_mod = types.ModuleType("tools.registry")
        reg_mod.tool_error = lambda msg: f"Error: {msg}"
        tools_mod.registry = reg_mod
        sys.modules["tools"] = tools_mod
        sys.modules["tools.registry"] = reg_mod

repo_root = Path(__file__).resolve().parent.parent
if __name__ == "__main__" and str(repo_root) not in sys.path:
    # CLI needs the plugin root for `from __init__ import ...`; in-process the
    # plugin's top-level names must not shadow the host's (tests/, scripts/).
    sys.path.insert(0, str(repo_root))


def _ingest_timeout_ms() -> int:
    """HERMES_ORACLE_INGEST_TIMEOUT (seconds, default 600) as ms, clamped to
    30-7200 s like the plugin; bad/non-positive values fall back to 600."""
    raw = (os.environ.get("HERMES_ORACLE_INGEST_TIMEOUT") or "").strip()
    try:
        secs = float(raw) if raw else 600.0
        if not (secs > 0 and secs != float("inf")):
            raise ValueError
    except ValueError:
        logger.warning("ignoring invalid HERMES_ORACLE_INGEST_TIMEOUT=%r; using 600", raw)
        secs = 600.0
    return int(min(max(secs, 30.0), 7200.0) * 1000)


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
    # Absolute but not resolved: a skill dir symlinked into a root keeps its
    # in-root path, which is what archive, prune and ownership checks look at
    # (Hermes archives the link, not its target).
    path = Path(os.path.abspath(os.path.expanduser(str(skill_path))))
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


def hermes_home() -> Path:
    """The active Hermes home (profile-aware). Inside Hermes this is
    hermes_constants.get_hermes_home(), which honours a per-context profile
    override; standalone it is $HERMES_HOME, else ~/.hermes."""
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

        return Path(get_hermes_home())
    except Exception:
        pass
    env = (os.environ.get("HERMES_HOME") or "").strip()
    return Path(os.path.expanduser(env)) if env else Path.home() / ".hermes"


def default_skill_roots() -> list[Path]:
    """Skill roots scanned by default, by the bulk sync and the lifecycle hook
    alike: the active profile's skills dir and the shared ~/.agents/skills."""
    out: list[Path] = []
    for d in (hermes_home() / "skills", Path.home() / ".agents" / "skills"):
        if d not in out:
            out.append(d)
    return out


def resolve_skill_roots(dirs: Sequence[str | Path] | None = None) -> list[Path]:
    """Existing, resolved, de-duplicated scan roots (*dirs* or the defaults)."""
    raw: Sequence[str | Path] = dirs if dirs else default_skill_roots()
    out: list[Path] = []
    for d in raw:
        p = Path(os.path.expanduser(os.path.expandvars(str(d)))).resolve()
        if p.is_dir() and p not in out:
            out.append(p)
    return out


def _under_any(p: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            p.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def path_in_roots(path: str | Path, roots: Sequence[str | Path]) -> bool:
    """True when *path* lies under one of *roots*, lexically or once resolved."""
    p = Path(os.path.abspath(str(path)))
    lexical = [Path(os.path.abspath(str(r))) for r in roots]
    if _under_any(p, lexical):
        return True
    try:
        return _under_any(p.resolve(), [r.resolve() for r in lexical])
    except OSError:
        return False


def same_file(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(str(a)).resolve() == Path(str(b)).resolve()
    except OSError:
        return False


def live_elsewhere(stored_path: Any, own_file: str | Path | None, roots: Sequence[str | Path]) -> bool:
    """True when *stored_path* is a SKILL.md that still exists, is not
    *own_file* and lies outside *roots*: another profile on this host (same
    host id) owns that copy, so this one must not replace or delete it."""
    if not stored_path:
        return False
    p = Path(str(stored_path))
    try:
        if not p.is_file():
            return False
    except OSError:
        return False
    if own_file is not None and same_file(p, own_file):
        return False
    return not path_in_roots(p, roots)


def copy_is_current(
    stored_hash: Any, stored_path: Any, skill_hash: str, skill_path: str | Path, roots: Sequence[str | Path]
) -> bool:
    """True when this host's stored copy has the file's hash and records the
    file's path. A copy whose path is gone or names another file is rewritten
    (it records where the skill lives now). The same file under another path is
    kept, except a target path outside the roots (rows written before 1.1.1.4
    for a skill dir symlinked in from outside), which is rewritten once to the
    in-root path so archive and prune can see it. Keeping other aliases means
    two profiles reaching one file by different paths never rewrite each other."""
    if not skill_hash or str(stored_hash or "") != skill_hash:
        return False
    if not stored_path:
        return True  # legacy row without a path: the hash decides
    sp = Path(str(stored_path))
    if str(sp) == str(skill_path):
        return True
    if not same_file(sp, skill_path):
        return False
    try:
        canonical = sp == sp.resolve()
    except OSError:
        canonical = False
    return not (canonical and not path_in_roots(sp, roots))


def archived_link_target(stored_path: Any, roots: Sequence[str | Path], dir_name: str) -> bool:
    """True when a root's .archive/ holds the archived symlink for *dir_name*
    (Hermes moves the link itself, flat, with an optional -<timestamp> suffix on
    collision) and it points at *stored_path*'s skill dir: the stored target
    still exists, but this profile's skill was archived."""
    if not stored_path or not dir_name:
        return False
    try:
        target = Path(str(stored_path)).parent.resolve()
    except OSError:
        return False
    stamped = re.compile(re.escape(dir_name) + r"(?:-\d{14})?$")
    for root in roots:
        arch = Path(str(root)) / ".archive"
        try:
            if not arch.is_dir():
                continue
            for cand in arch.iterdir():
                if stamped.match(cand.name) and cand.is_symlink() and cand.resolve() == target:
                    return True
        except OSError:
            continue
    return False


def discover_skills(dirs: list[str | Path] | None = None) -> list[SkillInfo]:
    """Scan directories for SKILL.md files and return parsed SkillInfo objects."""
    search_dirs = resolve_skill_roots(dirs)

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

                    skill_info = parse_skill_file(fpath)
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
        for d in default_skill_roots():
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
    """This host's identity in skill-corpus ids (skill:<host>:<name>:c<N>).
    HERMES_HOST_ID when set; otherwise the short host name, lower-cased (a
    shell's HOSTNAME may be the FQDN while a systemd service sees none, and
    both must give the same id). Keep identical to the plugin's get_host_id()."""
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


def like_escape(text: str) -> str:
    """Escape LIKE wildcards for `... LIKE :x ESCAPE '\\'`."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Every row of one skill owned by one host: ids skill:<host>:<name>:c<N>.
# Host and skill names cannot contain ':', so the prefix never matches another
# skill, another host's copy, or the host-less legacy corpus (skill:<name>[:cN]),
# and it is answered from the primary-key index.
OWN_SKILL_ROWS_SQL = "role = 'skill' AND memory_id LIKE :pfx ESCAPE '\\'"


def own_skill_rows_binds(host: str, name: str) -> dict[str, str]:
    if not host or not name or ":" in host or ":" in name:
        raise ValueError(f"invalid skill identity host={host!r} name={name!r}")
    return {"pfx": like_escape(f"skill:{host}:{name}:c") + "%"}


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
    # Meta and full rows share one host-qualified conv_ref (1.1.1.4).
    conv_ref = f"skill:{host}:{skill.name}" if host else skill.name

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
                "conv_ref": conv_ref,
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
    skills_or_mode: Any = None,
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
    prune: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Synchronize discovered skills into Oracle memory database.

    Every row written belongs to this host (skill:<host>:<name>:c<N>). Each
    changed skill is replaced in its own transaction (DELETE of this host's
    rows + INSERT, one commit), so a failure keeps the previous copy. Pruning
    is opt-in (*prune*, or *prune_host* = a path prefix): it deletes this
    host's skills whose SKILL.md is no longer under the scanned roots (or under
    *prune_host*), and never touches other hosts' copies or the host-less
    legacy corpus."""
    target_provider_or_conn = provider if provider is not None else provider_or_conn

    # skills_or_mode: a skill list, or a positional mode string. The keyword
    # *mode* applies otherwise (it used to be overridden by the positional
    # default "meta", so mode="full" silently ran a meta sync).
    if isinstance(skills_or_mode, (list, tuple)):
        skills = list(skills_or_mode)
    elif isinstance(skills_or_mode, str):
        mode = skills_or_mode
        skills = discover_skills(dirs=dirs)
    else:
        skills = discover_skills(dirs=dirs)

    if prune_host:
        prune_roots = [str(prune_host)]
    elif prune:
        prune_roots = [str(r) for r in resolve_skill_roots(dirs)] if not isinstance(skills_or_mode, (list, tuple)) \
            else ([host_root] if host_root else [])
    else:
        prune_roots = []

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
    # One identity for every skill row this host writes, deletes or prunes
    # (the lifecycle hook uses the same get_host_id()).
    effective_host = host_tag or get_host_id()

    stats: dict[str, Any] = {
        "status": "dry_run" if dry_run else "success",
        "scanned": len(skills),
        "indexed": 0,
        "updated": 0,
        "skipped": 0,
        "upserted": 0,
        "pruned": 0,
        "pruned_skills": [],
        "skipped_foreign": 0,
        "foreign_skills": [],
        "total_rows_inserted": 0,
        "errors": [],
        "mode": mode,
        "dry_run": dry_run,
        "host": effective_host,
    }
    # The roots this run scans: a stored copy of this host whose SKILL.md
    # still exists outside them belongs to another profile (same host id).
    owner_roots = resolve_skill_roots(dirs)

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
            ingest_ms = _ingest_timeout_ms()
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
                           json_value(metadata, '$.host') as host,
                           json_value(metadata, '$.total_chunks') as total_chunks
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
                    # full: True/False when the row says whether it holds the
                    # whole document (total_chunks), None when unknown.
                    sfull: bool | None = None
                    if len(row) >= 5:
                        sname = str(row[1] or "")
                        shash = str(row[2] or "")
                        spath = str(row[3] or "")
                        shost = str(row[4] or "")
                        if len(row) >= 6:
                            sfull = row[5] is not None
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

                    item = {"mid": mid, "name": sname, "hash": shash, "path": spath, "host": shost, "full": sfull}
                    if sname:
                        existing_by_name.setdefault(sname, []).append(item)
                    existing_map[mid] = item

                own_prefix = f"skill:{effective_host}:"
                to_upsert: list[SkillInfo] = []
                for skill in skills:
                    copies = existing_by_name.get(skill.name, [])
                    own = [c for c in copies if str(c.get("mid", "")).startswith(own_prefix)]
                    skill_hash = skill.hash or skill.content_hash
                    # This host's copy is stored from a SKILL.md that still
                    # exists outside the scanned roots: another profile owns
                    # it. Never replace it (not even with force); set
                    # HERMES_HOST_ID per profile to keep separate copies.
                    foreign = [c for c in own if live_elsewhere(c.get("path"), skill.path, owner_roots)]
                    if foreign:
                        stats["skipped_foreign"] += 1
                        stats["foreign_skills"].append({"name": skill.name, "path": foreign[0].get("path")})
                        logger.warning(
                            "skill '%s': this host's copy is stored from %s, which exists outside the scanned "
                            "roots (another profile); not replacing it", skill.name, foreign[0].get("path"),
                        )
                        continue

                    def _full_ok(c: dict[str, Any]) -> bool:
                        return mode != "full" or c.get("full") is not False

                    # Up to date: this host's copy has the file's hash and path
                    # (and, for a full sync, holds the whole document). With no
                    # copy of its own, an identical host-less shared-corpus
                    # copy is enough (it is never pruned); another host's copy
                    # is not, since that host may archive or prune it. A stale
                    # copy of its own is always replaced.
                    if own:
                        is_current = any(
                            copy_is_current(c.get("hash"), c.get("path"), skill_hash, skill.path, owner_roots)
                            and _full_ok(c)
                            for c in own
                        )
                    else:
                        is_current = bool(skill_hash) and any(
                            c.get("hash") == skill_hash and _full_ok(c) for c in copies if not c.get("host")
                        )
                    if not force and is_current:
                        stats["skipped"] += 1
                        continue
                    if own:
                        stats["updated"] += 1
                    else:
                        stats["indexed"] += 1
                    to_upsert.append(skill)

                use_provider_insert = hasattr(target_provider_or_conn, "_insert_many") and callable(
                    getattr(target_provider_or_conn, "_insert_many")
                )
                del_sql = f"DELETE /*+ NO_PARALLEL */ FROM {table} WHERE {OWN_SKILL_ROWS_SQL}"
                insert_sql = f"""
                    INSERT INTO {table}
                        (memory_id, session_id, agent_id, role, content,
                         created_at, target, metadata, content_txt, conv_ref)
                    VALUES (:1, :2, :3, :4, :5, SYSTIMESTAMP, :6, :7, :8, :9)
                """
                for skill in to_upsert:
                    rows = format_skill_payload(skill, mode=mode, host=effective_host)
                    stats["upserted"] += 1
                    if dry_run or not rows:
                        continue
                    # One transaction per skill on this connection: this host's
                    # old rows go and the new ones arrive in the same commit.
                    backfill: list[tuple[str, str]] = []
                    try:
                        cur.execute(del_sql, own_skill_rows_binds(effective_host, skill.name))
                        if use_provider_insert:
                            backfill = target_provider_or_conn._insert_many(rows, tx_conn=conn) or []
                        else:
                            binds = [
                                (
                                    r["memory_id"],
                                    r.get("session_id", "skills"),
                                    effective_host,
                                    "skill",
                                    r["content"],
                                    "memory",
                                    json.dumps(r["metadata"]),
                                    (r["content"] or "")[:4000],
                                    r.get("conv_ref"),
                                )
                                for r in rows
                            ]
                            for i in range(0, len(binds), max(1, batch_size)):
                                cur.executemany(insert_sql, binds[i : i + max(1, batch_size)])
                        conn.commit()
                    except Exception as e:
                        # This skill keeps its previous rows. Carry on with the
                        # rest, but give up after 5 failures (likely a broken session).
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        stats["errors"].append(f"{skill.name}: {e}")
                        stats["upserted"] -= 1
                        if len(stats["errors"]) >= 5:
                            raise
                        continue
                    except BaseException:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        raise
                    stats["total_rows_inserted"] += len(rows)
                    queue = getattr(target_provider_or_conn, "_queue_embed_backfill", None)
                    if callable(queue):
                        for mid, cnt in backfill if isinstance(backfill, list) else []:
                            queue(mid, cnt)

                # Host-scoped pruning: only this host's skills (own id prefix)
                # whose SKILL.md sat under a pruned root and is no longer found.
                if prune_roots:
                    alive = {s.name for s in skills}
                    root_binds = {
                        f"r{i}": like_escape(r.rstrip("/\\") + os.sep) + "%" for i, r in enumerate(prune_roots)
                    }
                    root_sql = " OR ".join(
                        f"json_value(metadata, '$.path') LIKE :r{i} ESCAPE '\\'" for i in range(len(prune_roots))
                    )
                    stale_sql = f"""
                        SELECT DISTINCT json_value(metadata, '$.skill_name')
                        FROM {table}
                        WHERE role = 'skill' AND memory_id LIKE :own ESCAPE '\\' AND ({root_sql})
                    """
                    cur.execute(stale_sql, {"own": like_escape(own_prefix) + "%", **root_binds})
                    stale = sorted({r[0] for r in cur.fetchall() if r and r[0] and r[0] not in alive})
                    stats["prune_candidates"] = len(stale)
                    for name in stale[: max(0, prune_limit)]:
                        stats["pruned_skills"].append(name)
                        stats["pruned"] += 1
                        if not dry_run:
                            cur.execute(del_sql, own_skill_rows_binds(effective_host, name))
                    if not dry_run and stats["pruned"]:
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

    if stats["errors"] and stats["status"] == "success":
        stats["status"] = "partial"
    return stats


_LEGACY_ID_RE = re.compile(r"^skill:([^:]+)(?::c(\d+))?$")
_HOSTED_ID_RE = re.compile(r"^skill:([^:]+):([^:]+):c\d+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_NO_HOST = "json_value(metadata, '$.host') IS NULL"


def list_local_skills(dirs: list[str | Path] | None = None) -> dict[str, Any]:
    """This host's identity and live skills (name + SKILL.md path), as used by
    migrate_host_identity(). No database access."""
    return {
        "host": get_host_id(),
        "skills": [
            {"name": s.name, "path": str(s.path), "real_path": str(Path(s.path).resolve())}
            for s in discover_skills(dirs=dirs)
        ],
    }


def migrate_host_identity(
    conn: Any,
    table: str,
    listings: Sequence[dict[str, Any]],
    dry_run: bool = True,
) -> dict[str, Any]:
    """Give legacy (host-less) skill rows their owner's identity.

    *listings* are list_local_skills() outputs, one per host. A legacy skill is
    relabelled only when one listing has a live SKILL.md with the same name AT
    THE SAME PATH (in-root or resolved); its rows become skill:<host>:<name>:c<N> (a bare skill:<name>
    becomes :c0) with conv_ref skill:<host>:<name> and metadata.host, and keep
    metadata.legacy_id / legacy_conv_ref for reverse_host_identity(). Everything
    else stays legacy: the shared corpus (archive rows, skills whose files are
    gone) is never relabelled, so host-scoped pruning can never reach it. A
    skill the host already stores under its own identity is reported as
    superseded and left alone. One transaction; nothing is written on dry_run."""
    owners: dict[tuple[str, str], set[str]] = {}
    for listing in listings:
        host = str(listing.get("host") or "")
        if not _HOST_RE.match(host):
            raise ValueError(f"invalid host in listing: {host!r}")
        for s in listing.get("skills") or []:
            # Legacy rows recorded the resolved path; listings carry both.
            for key in ("path", "real_path"):
                if s.get(key):
                    owners.setdefault((str(s.get("name") or ""), str(s[key])), set()).add(host)

    report: dict[str, Any] = {
        "dry_run": dry_run,
        "relabelled_skills": {},
        "relabelled_rows": 0,
        "superseded": [],
        "ambiguous": [],
        "skipped": [],
        "legacy_skills_total": 0,
        "left_shared_skills": 0,
    }
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT memory_id, conv_ref, json_value(metadata, '$.skill_name'), json_value(metadata, '$.path')
                FROM {table} WHERE role = 'skill' AND {_NO_HOST}"""  # nosec B608
        )
        legacy: dict[tuple[str, str], list[tuple[str, Any]]] = {}
        for mid, conv_ref, name, path in cur.fetchall():
            legacy.setdefault((str(name or ""), str(path or "")), []).append((str(mid), conv_ref))
        cur.execute(
            f"SELECT memory_id FROM {table} WHERE role = 'skill' AND regexp_like(memory_id, '^skill:[^:]+:[^:]+:c[0-9]+$')"  # nosec B608
        )
        hosted = set()
        for (mid,) in cur.fetchall():
            m = _HOSTED_ID_RE.match(str(mid))
            if m:
                hosted.add((m.group(1), m.group(2)))

        report["legacy_skills_total"] = len(legacy)
        plan: list[dict[str, Any]] = []
        for (name, path), rows in sorted(legacy.items()):
            hosts = owners.get((name, path))
            if not hosts:
                continue
            if len(hosts) > 1:
                report["ambiguous"].append({"skill": name, "hosts": sorted(hosts)})
                continue
            host = next(iter(hosts))
            if (host, name) in hosted:
                report["superseded"].append({"skill": name, "host": host})
                continue
            new_ids = []
            for mid, _ in rows:
                m = _LEGACY_ID_RE.match(mid)
                new_ids.append(f"skill:{host}:{name}:c{m.group(2) or 0}" if m and m.group(1) == name else None)
            if None in new_ids or len(set(new_ids)) != len(new_ids):
                report["skipped"].append({"skill": name, "reason": "unexpected or colliding ids"})
                continue
            for (mid, conv_ref), new_id in zip(rows, new_ids):
                plan.append({
                    "new_id": new_id,
                    "new_ref": f"skill:{host}:{name}",
                    "host": host,
                    "old_id": mid,
                    "old_ref": conv_ref,
                })
            report["relabelled_skills"].setdefault(host, []).append(name)
            report["relabelled_rows"] += len(rows)
        report["left_shared_skills"] = len(legacy) - sum(len(v) for v in report["relabelled_skills"].values())
        report["relabelled_skill_counts"] = {h: len(v) for h, v in report["relabelled_skills"].items()}

        if dry_run or not plan:
            return report
        sql = f"""
            UPDATE {table}
            SET memory_id = :new_id, conv_ref = :new_ref,
                metadata = JSON_TRANSFORM(COALESCE(metadata, JSON('{{}}')),
                                          SET '$.host' = :host,
                                          SET '$.legacy_id' = :old_id,
                                          SET '$.legacy_conv_ref' = :old_ref)
            WHERE memory_id = :old_id AND role = 'skill' AND {_NO_HOST}
        """  # nosec B608
        try:
            cur.executemany(sql, plan)
            if (cur.rowcount or 0) != len(plan):
                raise RuntimeError(f"migrate_host_identity: updated {cur.rowcount} rows, planned {len(plan)}; rolled back")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    logger.info("migrate_host_identity: relabelled %d rows", report["relabelled_rows"])
    return report


def reverse_host_identity(conn: Any, table: str, dry_run: bool = True) -> dict[str, Any]:
    """Undo migrate_host_identity(): every row carrying metadata.legacy_id gets
    its legacy id and conv_ref back and loses host/legacy_* metadata. Rows that
    a later sync replaced no longer carry legacy_id and are not restored (keep
    a table backup for that). One transaction; nothing is written on dry_run."""
    report: dict[str, Any] = {"dry_run": dry_run, "restored_rows": 0, "collisions": []}
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT memory_id, json_value(metadata, '$.legacy_id'), json_value(metadata, '$.legacy_conv_ref')
                FROM {table}
                WHERE role = 'skill'
                  AND json_value(metadata, '$.legacy_id') IS NOT NULL"""  # nosec B608
        )
        rows = cur.fetchall()
        plan = []
        if rows:
            cur.execute(
                f"SELECT memory_id FROM {table} WHERE role = 'skill' AND regexp_like(memory_id, '^skill:[^:]+(:c[0-9]+)?$')"  # nosec B608
            )
            taken = {str(r[0]) for r in cur.fetchall()}
            for mid, old_id, old_ref in rows:
                if not old_id or old_id in taken:
                    report["collisions"].append({"memory_id": mid, "legacy_id": old_id})
                    continue
                plan.append({"cur_id": mid, "old_id": old_id, "old_ref": old_ref})
        report["restored_rows"] = len(plan)
        if dry_run or not plan:
            return report
        sql = f"""
            UPDATE {table}
            SET memory_id = :old_id, conv_ref = :old_ref,
                metadata = JSON_TRANSFORM(metadata, REMOVE '$.host', REMOVE '$.legacy_id', REMOVE '$.legacy_conv_ref')
            WHERE memory_id = :cur_id AND role = 'skill'
        """  # nosec B608
        try:
            cur.executemany(sql, plan)
            if (cur.rowcount or 0) != len(plan):
                raise RuntimeError(f"reverse_host_identity: updated {cur.rowcount} rows, planned {len(plan)}; rolled back")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync skills library into Oracle 26ai vector memory.")
    parser.add_argument("--mode", choices=["meta", "full"], default="meta", help="Ingestion mode: meta or full (default: meta)")
    parser.add_argument("--dirs", nargs="*", default=None, help="Custom directories to scan for skills")
    parser.add_argument("--force", action="store_true", help="Force re-indexing even if already up to date")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without modifying the database")
    parser.add_argument("--batch-size", type=int, default=50, help="Batch size for database inserts (default: 50)")
    parser.add_argument("--table", default=None, help="Oracle memory table name override")
    parser.add_argument("--prune", action="store_true", help="Delete this host's skills that are no longer under the scanned roots")
    parser.add_argument("--prune-host", default=None, help="Prune this host's skills whose SKILL.md path starts with this prefix")
    parser.add_argument("--prune-limit", type=int, default=500, help="Maximum number of stale skills to prune in one run (default: 500)")
    parser.add_argument("--list-local", action="store_true", help="Print this host's id and live skills as JSON (no database)")
    parser.add_argument("--migrate-host-identity", action="store_true",
                        help="Relabel legacy (host-less) skill rows whose SKILL.md is live on a host; see --listing")
    parser.add_argument("--listing", action="append", default=[],
                        help="--list-local output file of a host (repeatable; default: this host)")
    parser.add_argument("--reverse-host-identity", action="store_true", help="Undo --migrate-host-identity")
    parser.add_argument("--apply", action="store_true",
                        help="Write the --migrate-host-identity / --reverse-host-identity changes (default: dry run)")
    parser.add_argument("--env-file", default=None,
                        help="Load KEY=VALUE settings (OCI_DB_*) from this file; by default only the environment is read")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.list_local:
        print(json.dumps(list_local_skills(dirs=args.dirs), indent=2))
        return
    if args.env_file:
        load_env_file(Path(args.env_file))

    if args.dry_run and not (args.migrate_host_identity or args.reverse_host_identity):
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
        if args.dry_run and not (args.migrate_host_identity or args.reverse_host_identity):
            sys.exit(0)
        print(f"Error: Oracle memory provider unavailable: {provider.unavailable_reason()}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.migrate_host_identity or args.reverse_host_identity:
            table_name = args.table or provider._table
            with provider._pool.acquire() as conn:
                conn.call_timeout = _ingest_timeout_ms()
                # Migrations are dry runs unless --apply is given.
                dry = args.dry_run or not args.apply
                if args.reverse_host_identity:
                    rep = reverse_host_identity(conn, table_name, dry_run=dry)
                else:
                    listings = [json.loads(Path(f).read_text(encoding="utf-8")) for f in args.listing] \
                        or [list_local_skills(dirs=args.dirs)]
                    rep = migrate_host_identity(conn, table_name, listings, dry_run=dry)
            print(json.dumps(rep, indent=2))
            return

        res = sync_skills_library(
            provider=provider,
            mode=args.mode,
            force=args.force,
            dirs=args.dirs,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            prune=args.prune,
            prune_host=args.prune_host,
            prune_limit=args.prune_limit,
            host_tag=get_host_id(),
        )
        print("Sync complete:")
        print(json.dumps(res, indent=2))
    finally:
        provider.shutdown()


if __name__ == "__main__":
    main()
