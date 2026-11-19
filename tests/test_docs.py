"""README.md and INSTALL.md say what the code does (#23). No DB."""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
README = (REPO / "README.md").read_text(encoding="utf-8")
INSTALL = (REPO / "INSTALL.md").read_text(encoding="utf-8")
SOURCE = (REPO / "__init__.py").read_text(encoding="utf-8")

# Settings the provider reads: _setting("X"), _env_number("X", ...),
# _env_number_opt("X"), get_secret("X"), os.environ.get("X").
_READ_RE = re.compile(
    r"""(?:_setting|_env_number|_env_number_opt|get_secret|environ\.get)\(\s*["']"""
    r"""((?:HERMES_ORACLE_|OCI_|HERMES_HOST_ID|TNS_ADMIN)[A-Z0-9_]*)["']"""
)
# Read by the code, deliberately not in the table.
_UNDOCUMENTED = {
    "HERMES_ORACLE_SEARCH_TIMEOUT",  # pre-1.1.1.3 name in seconds; _MS replaces it
}


def _code_blocks(text: str) -> list[str]:
    return re.findall(r"```[a-z]*\n(.*?)```", text, flags=re.S)


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = re.search(r"(?m)^## ", text[start + len(heading):])
    return text[start:start + len(heading) + nxt.start()] if nxt else text[start:]


def _table_settings(text: str) -> set[str]:
    return set(re.findall(r"(?m)^\| `([A-Z][A-Z0-9_]+)` \|", text))


class TestInstallPath(unittest.TestCase):
    """#23: one install path, through the Hermes plugin manager, which
    provisions the Python dependency in the environment Hermes imports from."""

    def test_readme_installs_through_the_plugin_manager(self):
        install = _section(README, "## Install")
        self.assertIn("hermes plugins install xl3v3r/hermes-oracle-memory --yes-deps", install)
        self.assertIn("hermes plugins enable oracle", install)
        self.assertRegex(install, r"(?i)restart")
        for block in _code_blocks(README):
            self.assertNotIn("pip install", block)

    def test_install_guide_installs_through_the_plugin_manager(self):
        step = _section(INSTALL, "## Step 1")
        self.assertIn("hermes plugins install xl3v3r/hermes-oracle-memory --yes-deps", step)
        self.assertIn("hermes plugins enable oracle", step)
        for block in _code_blocks(step):
            self.assertNotIn("pip install", block)
        self.assertRegex(_section(INSTALL, "## Step 2"), r"(?i)restart")

    def test_no_dependency_that_nothing_imports(self):
        for name, text in (("README.md", README), ("INSTALL.md", INSTALL)):
            for block in _code_blocks(text):
                for line in block.splitlines():
                    if "pip install" in line:
                        self.assertNotRegex(line, r"\boci\b", f"{name}: {line}")
                        self.assertNotIn("-e .", line, f"{name}: {line}")

    def test_the_documented_message_is_the_one_the_provider_gives(self):
        m = re.search(r"oracledb is not importable[^\n\"]*", INSTALL)
        assert m is not None
        self.assertIn(m.group(0).strip(), re.sub(r'"\s*\n\s*"', "", SOURCE))

    def test_env_file_is_named_through_hermes_home(self):
        for name, text in (("README.md", README), ("INSTALL.md", INSTALL)):
            for line in text.splitlines():
                if "~/.hermes/.env" in line:
                    self.assertIn("$HERMES_HOME/.env", line, f"{name}: {line[:120]}")


if __name__ == "__main__":
    unittest.main()
