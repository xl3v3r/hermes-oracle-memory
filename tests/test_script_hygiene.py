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


if __name__ == "__main__":
    unittest.main()
