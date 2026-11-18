"""Live database check, opt-in.

Runs tests/verify_provider.py against the database named by OCI_DB_USER,
OCI_DB_PASSWORD and OCI_DB_DSN, and only when HERMES_ORACLE_LIVE_TESTS=1 is
set. Credentials in the environment, or an env file on disk, are not enough:
a plain `pytest` run never connects to a database. The script deletes every
row it writes.

    HERMES_ORACLE_LIVE_TESTS=1 python -m pytest -q tests/test_provider_live.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401  (stubs agent.* / tools.*)
import tests.verify_provider as verify_provider  # noqa: E402


class TestProviderLive(unittest.TestCase):
    def test_verify_provider_live(self):
        if os.environ.get("HERMES_ORACLE_LIVE_TESTS") != "1":
            self.skipTest(
                "set HERMES_ORACLE_LIVE_TESTS=1 (with OCI_DB_USER, OCI_DB_PASSWORD and OCI_DB_DSN) "
                "to run against a database"
            )
        self.assertEqual(verify_provider.main([]), 0, "tests/verify_provider.py reported a failure")


if __name__ == "__main__":
    unittest.main()
