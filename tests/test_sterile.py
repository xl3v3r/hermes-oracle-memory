"""#34: nothing site-specific in the repository. No DB.

Three checks over the tracked files:

1. Paths that must not be tracked (dev-tool hook and agent files).
2. Terms that were removed. They are kept as SHA-256 digests, so this file
   does not repeat them. To block another term, add the output of
   `python tests/test_sterile.py --digest "<term>"` to _BLOCKED.
3. Shapes that are site-specific whatever their value: cloud region ids,
   OCIDs and email addresses.
"""
import hashlib
import os
import re
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# First path segment, or file name, of things that are not part of the plugin.
_FORBIDDEN_DIRS = frozenset({".agents", ".entire", ".claude", ".jules", ".cursor"})
_FORBIDDEN_NAMES = frozenset({"AGENTS.md", "NEEDS.md", "CLAUDE.md"})

# The CLA bot's storage holds the names of the people who signed.
_NOT_SCANNED = ("signatures/",)

_SKIP_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", "build", "dist", ".venv"})

_COMPOUND_RE = re.compile(r"[a-z0-9]+(?:[-_.,][a-z0-9]+)*")
_ATOM_RE = re.compile(r"[a-z0-9]+")
_MAX_ATOMS = 4

# sha256 of the normalised term (see _normalise).
_BLOCKED = frozenset({
    "15d812cf61a26dc218739a43a65122165569a5667f751e7172d74e79f0c59806",
    "1ef7ae41aa689706e5d5c6763f5c74972fa547a6fff0c670222c14da5442e198",
    "29c158c83715d98404f4a3c3e5a3978a24fad26ba59cc25dfa6300c4a632b7d6",
    "4bc4c4b6671e39aa9b59b7cdce1bef44e68468396831e36da10b3d5b4e1e5740",
    "4c713b660433b668d55b00b87f5c64ce2ad5aeb94207d3fbfc51634feefe9088",
    "65b73f818d9cb50ff4f456c9607660df95919dae3184730bc30e0fc545618a0e",
    "8f6a9f6e2c6f241a1e8a2bb6cc81aa91c0f4bdbd04647468ad274798521039b3",
    "96a4bc2602655473120fcc571ee3d8cfe5f8801f8038ccc06323d305e323331c",
    "a42bd9768ab78d0c2a346dfe6c42bfa5d0db6869a4214ba93e31fdd2ea56e33c",
    "afa71fd643a35ef93ea31e12d5502f72587e8a462f92b5b4582e2c051ff04771",
    "b53f301148bbbbfd414229124c2a3f6d8453023d03e14fc4ec5f679b2b3e8db8",
    "be823b1486cd0f861df83a9cb744ae9a0a3c68fbae2c5c8e3cfec9a3ca872b61",
    "c2fef3878f02033fffaf58c69cfed22a28414487700df17170964952736267a1",
    "c70f8edd66571775ed28895725defdefdf372639aeb625fdc55bbd28105cbe6c",
    "fc75bb468877a83692705d1351c6e876fdc9ee20d0fcecc38c9cee8f84388952",
})

_SHAPES = (
    ("cloud region id", re.compile(r"\b[a-z]{2}-[a-z]{4,}-\d\b")),
    ("OCID", re.compile(r"\bocid1\.[a-z0-9]+\.")),
    ("email address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[a-z]{2,}\b")),
)


def _normalise(term: str) -> str:
    """Lower case, atoms joined by '-': 'Some_Name.md' -> 'some-name-md'."""
    return "-".join(_ATOM_RE.findall(term.lower()))


def _digest(term: str) -> str:
    return hashlib.sha256(_normalise(term).encode("utf-8")).hexdigest()


def _candidates(text: str):
    """Every run of 1.._MAX_ATOMS atoms inside one unbroken token, with the
    position of the token (for the line number)."""
    for m in _COMPOUND_RE.finditer(text.lower()):
        atoms = _ATOM_RE.findall(m.group(0))
        for i in range(len(atoms)):
            for n in range(1, _MAX_ATOMS + 1):
                if i + n > len(atoms):
                    break
                yield m.start(), "-".join(atoms[i:i + n])


def _tracked_files() -> list[str]:
    if (REPO / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                capture_output=True, check=True,
            ).stdout.decode("utf-8")
            # a file deleted in the work tree is still listed until it is staged
            return sorted(f for f in out.split("\0") if f and (REPO / f).is_file())
        except (OSError, subprocess.CalledProcessError):
            pass
    found = []
    for root, dirs, names in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.endswith(".egg-info")]
        for n in names:
            if not n.endswith((".pyc", ".pyo")):
                found.append(str((Path(root) / n).relative_to(REPO)).replace(os.sep, "/"))
    return sorted(found)


def _read(rel: str) -> str | None:
    try:
        return (REPO / rel).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def blocked_terms(text: str) -> list[tuple[int, str]]:
    """(line, digest prefix) of every blocked term in text. The term itself is
    not reported: the output of a failing CI run is public."""
    hits = []
    for pos, cand in _candidates(text):
        d = hashlib.sha256(cand.encode("utf-8")).hexdigest()
        if d in _BLOCKED:
            hits.append((_line_of(text, pos), d[:12]))
    return sorted(set(hits))


class TestSterileTree(unittest.TestCase):
    def test_dev_tool_files_are_not_tracked(self):
        bad = [
            f for f in _tracked_files()
            if f.split("/", 1)[0] in _FORBIDDEN_DIRS or f.rsplit("/", 1)[-1] in _FORBIDDEN_NAMES
        ]
        self.assertEqual(bad, [])

    def test_no_removed_term_is_back(self):
        bad = []
        for f in _tracked_files():
            if f.startswith(_NOT_SCANNED):
                continue
            text = _read(f)
            if text is None:
                continue
            bad.extend(f"{f}:{line} (term {d})" for line, d in blocked_terms(text))
            bad.extend(f"{f} (file name, term {d})" for _line, d in blocked_terms(f))
        self.assertEqual(bad, [], "see the module docstring of tests/test_sterile.py")

    def test_no_site_specific_shapes(self):
        bad = []
        for f in _tracked_files():
            if f.startswith(_NOT_SCANNED):
                continue
            text = _read(f)
            if text is None:
                continue
            for label, rx in _SHAPES:
                for m in rx.finditer(text):
                    bad.append(f"{f}:{_line_of(text, m.start())} ({label})")
        self.assertEqual(bad, [])


class TestLicensorName(unittest.TestCase):
    """LICENSE, CLA.md and pyproject.toml name one licensor, the same way."""

    def _names(self):
        patterns = {
            "LICENSE": r"^Licensor: (.+)$",
            "CLA.md": r'"Licensor"\*\* means (.+?) and authorized successors\.',
            "pyproject.toml": r'^authors = \[\s*\{name = "([^"]+)"\}\s*\]',
        }
        out = {}
        for f, rx in patterns.items():
            m = re.search(rx, _read(f) or "", re.M)
            self.assertIsNotNone(m, f"{f}: no licensor line")
            out[f] = m.group(1)
        return out

    def test_one_name_in_all_three_files(self):
        names = self._names()
        self.assertEqual(len(set(names.values())), 1, names)

    def test_name_is_a_plain_name(self):
        # No handle or alias after the name.
        for f, name in self._names().items():
            self.assertRegex(name, r"^[A-Z][a-z]+(?: [A-Z][a-z]+)+$", f)


class TestTheCheckItself(unittest.TestCase):
    """The scan must find a term in the forms it can take in a file."""

    def test_normalise(self):
        self.assertEqual(_normalise("Some_Name.md"), "some-name-md")
        self.assertEqual(_normalise("12,345"), "12-345")

    def test_a_term_is_found_inside_longer_tokens_and_in_any_case(self):
        term = "Zq-Example_Term"
        with unittest.mock.patch(f"{__name__}._BLOCKED", frozenset({_digest(term)})):
            for text in (
                "x zq-example-term y",
                "id skill:ZQ-EXAMPLE-TERM:c0",
                "path ~/skills/zq_example_term/SKILL.md",
                "prefix-zq-example-term-suffix",
            ):
                self.assertEqual(len(blocked_terms(text)), 1, text)
            self.assertEqual(blocked_terms("zq example term"), [])
            self.assertEqual(blocked_terms("line one\nline two zq-example-term"), [(2, _digest(term)[:12])])

    def test_shapes(self):
        region, ocid, mail = (rx for _label, rx in _SHAPES)
        self.assertTrue(region.search("host adb." + "xx-" + "sometown-1" + ".example"))
        self.assertFalse(region.search("a re-run-1 of the job, or utf-8"))
        self.assertTrue(ocid.search("ocid1" + ".tenancy.oc1..aaaa"))
        self.assertTrue(mail.search("someone" + "@" + "example.org"))
        self.assertFalse(mail.search("git@github or user@host"))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--digest":
        print(_digest(sys.argv[2]))
    else:
        unittest.main()
