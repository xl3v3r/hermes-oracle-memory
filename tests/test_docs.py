"""README.md and INSTALL.md say what the code does (#23, #33). No DB."""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
README = (REPO / "README.md").read_text(encoding="utf-8")
INSTALL = (REPO / "INSTALL.md").read_text(encoding="utf-8")
SOURCE = (REPO / "__init__.py").read_text(encoding="utf-8")

# Every setting name the provider names as a string literal: the argument of
# _setting(), _env_number(), get_secret(), os.environ.get(), or a member of
# the tuple a loop reads.
_READ_RE = re.compile(r"""["']((?:HERMES_ORACLE_|OCI_|HERMES_HOST_ID|TNS_ADMIN)[A-Z0-9_]*)["']""")
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


class TestReadmeCorrections(unittest.TestCase):
    """#33."""

    def test_every_setting_the_provider_reads_is_in_the_table(self):
        read = set(_READ_RE.findall(SOURCE)) - _UNDOCUMENTED
        self.assertGreater(len(read), 20)
        self.assertIn("TNS_ADMIN", read)
        self.assertEqual(sorted(read - _table_settings(README)), [])

    def test_the_table_lists_no_setting_the_provider_does_not_read(self):
        scripts = "".join(p.read_text(encoding="utf-8") for p in sorted((REPO / "scripts").glob("*.py")))
        for name in sorted(_table_settings(README)):
            self.assertRegex(SOURCE + scripts, rf"""["']{name}["']""", name)

    def test_defaults_in_the_table(self):
        rows = dict(re.findall(r"(?m)^\| `([A-Z][A-Z0-9_]+)` \| ([^|]*) \|", README))
        # required: is_available() refuses to start without it
        self.assertNotIn("your-db-user", rows["OCI_DB_USER"])
        self.assertRegex(rows["OCI_DB_USER"], r"required")
        self.assertRegex(rows["HERMES_HOST_ID"], r"(?i)host name")
        self.assertNotIn("System user", rows["HERMES_HOST_ID"])
        self.assertIn("30000", rows["HERMES_ORACLE_SEARCH_TIMEOUT_MS"])

    def test_no_stale_timeout_default(self):
        self.assertNotRegex(README, r"13000|13 s\b")

    def test_release_history_lives_in_the_changelog(self):
        self.assertEqual(len(re.findall(r"(?m)^## What's new", README)), 0)
        self.assertIn("CHANGELOG.md", README)

    def test_no_notice_block(self):
        manifest = (REPO / "plugin.yaml").read_text(encoding="utf-8")
        for name, text in (("README.md", README), ("INSTALL.md", INSTALL), ("plugin.yaml", manifest)):
            self.assertNotRegex(text, r"(?i)criticality notice", name)

    def test_license_badge_uses_the_spdx_identifier(self):
        for name, text in (("README.md", README), ("INSTALL.md", INSTALL)):
            self.assertIn("license-BUSL--1.1-", text, name)
            self.assertNotIn("license-BSL--1.1-", text, name)

    def test_no_one_item_numbered_list(self):
        for name, text in (("README.md", README), ("INSTALL.md", INSTALL)):
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if not line.startswith("1. "):
                    continue
                second = False
                for nxt in lines[i + 1:]:
                    if nxt.startswith("2. "):
                        second = True
                        break
                    if nxt and not nxt[0].isspace():
                        break  # a new paragraph or heading: the list is over
                self.assertTrue(second, f"{name}: one-item numbered list: {line[:80]!r}")


class TestInstallCorrections(unittest.TestCase):
    """#33: INSTALL.md described code that does not exist."""

    def test_no_feature_the_code_does_not_have(self):
        for stale in ("_embed_many", "GenAI", "Instance Principal", "signer", "fallback embedding",
                      "thin/thick", "fallback routing"):
            self.assertNotIn(stale.lower(), INSTALL.lower(), stale)

    def test_embeddings_are_described_as_in_database_only(self):
        self.assertIn("VECTOR_EMBEDDING", INSTALL)


if __name__ == "__main__":
    unittest.main()
