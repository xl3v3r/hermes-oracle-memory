"""Helper-script and packaging hygiene (#12). No DB."""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import tests.conftest  # noqa: F401

REPO = Path(root_dir)


class TestEnvFileHygiene(unittest.TestCase):
    """#12: helper scripts read OCI_* from the environment; a file is loaded
    only with an explicit --env-file."""

    def test_no_script_reads_the_hermes_env_file_by_default(self):
        for f in sorted((REPO / "scripts").glob("*.py")):
            src = f.read_text(encoding="utf-8")
            self.assertNotRegex(src, r'Path\.home\(\)\s*/\s*"\.hermes"\s*/\s*"\.env"', f.name)
            if "def load_env" in src:
                self.assertIn("--env-file", src, f.name)

    def test_load_env_requires_a_path(self):
        from scripts import audit_embeddings

        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HOM_TEST_KEY", None)
            audit_embeddings.load_env(None)
            self.assertNotIn("HOM_TEST_KEY", os.environ)
            env = Path(td) / "e"
            env.write_text("export HOM_TEST_KEY='v1'\n# c\n")
            audit_embeddings.load_env(str(env))
            self.assertEqual(os.environ.pop("HOM_TEST_KEY"), "v1")

    def test_packaging_metadata(self):
        py = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(py, r'(?m)^license = "BUSL-1\.1"$')
        self.assertIn('license-files = ["LICENSE"]', py)
        cla = (REPO / ".github" / "workflows" / "cla.yml").read_text(encoding="utf-8")
        m = re.search(r"allowlist: '([^']*)'", cla)
        assert m is not None
        allow = m.group(1).split(",")
        self.assertNotIn("*[bot]", allow)
        self.assertTrue(all("*" not in a for a in allow))


class TestPackaging(unittest.TestCase):
    """#25: the wheel carries what the provider loads at run time. The wheel
    itself is built, installed and checked in CI (tests/check_wheel.py)."""

    def _package_data(self):
        py = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r"(?m)^hermes_oracle_memory = \[(.*)\]$", py)
        assert m is not None, "package-data for hermes_oracle_memory not found"
        return [x.strip().strip('"') for x in m.group(1).split(",")]

    def test_runtime_files_are_package_data(self):
        patterns = self._package_data()
        self.assertIn("plugin.yaml", patterns)
        self.assertIn("scripts/*.py", patterns)
        # every file the provider or the registration reads from its own directory
        import fnmatch

        for rel in ("scripts/sync_skills.py", "scripts/_common.py",
                    "skills/oracle-import/SKILL.md", "skills/oracle-skill-corpus/SKILL.md"):
            self.assertTrue((REPO / rel).is_file(), rel)
            self.assertTrue(any(fnmatch.fnmatch(rel, pat) for pat in patterns), rel)

    def test_package_data_cannot_pick_up_compiled_files(self):
        import fnmatch

        for pat in self._package_data():
            self.assertNotIn("**", pat, pat)
            self.assertFalse(pat.endswith("*"), pat)
            for junk in ("scripts/__pycache__/sync_skills.cpython-313.pyc",
                         "skills/oracle-import/__pycache__/x.cpython-313.pyc"):
                self.assertFalse(fnmatch.fnmatch(junk, pat) and "/" not in pat.split("*")[-1], (pat, junk))

    def test_wheel_check_requires_files_that_exist(self):
        src = (REPO / "tests" / "check_wheel.py").read_text(encoding="utf-8")
        required = re.findall(r'^    "([^"]+)",$', src, re.M)
        self.assertGreaterEqual(len(required), 5)
        for rel in required:
            self.assertTrue((REPO / rel).is_file(), rel)

    def test_manifest_names_no_missing_file(self):
        for line in (REPO / "MANIFEST.in").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in ("include", "exclude") and "*" not in parts[1]:
                self.assertTrue((REPO / parts[1]).exists(), line)

    def test_ci_builds_and_installs_the_wheel(self):
        wf = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("python -m build", wf)
        self.assertIn("check_wheel.py", wf)

    def test_manifest_declares_a_hermes_floor(self):
        manifest = (REPO / "plugin.yaml").read_text(encoding="utf-8")
        self.assertRegex(manifest, r'(?m)^requires_hermes: ">=0\.\d+\.\d+"$')


if __name__ == "__main__":
    unittest.main()
