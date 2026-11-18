"""The unit suite is hermetic (#19, #22): it cannot reach a database, whatever
the calling shell exports. tests/conftest.py drops inherited database settings
and makes the driver's connect functions refuse."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: E402,F401
import __init__ as oracle_plugin  # noqa: E402

CHECKOUT = Path(__file__).resolve().parent.parent
LEAKED = {
    "OCI_DB_USER": "LIVE_USER", "OCI_DB_PASSWORD": "live-password", "OCI_DB_DSN": "live.example.com:1522/prod",
    "OCI_WALLET_LOCATION": "/live/wallet", "OCI_WALLET_PASSWORD": "wp", "TNS_ADMIN": "/live/tns",
}
PROBE = (
    "import os, sys, tests.conftest, oracledb\n"
    "kept = sorted(k for k in os.environ if k.startswith('OCI_') or k == 'TNS_ADMIN')\n"
    "try:\n"
    "    oracledb.connect(user='u', password='p', dsn='127.0.0.1:1/none', tcp_connect_timeout=1)\n"
    "    how = 'connected'\n"
    "except AssertionError:\n"
    "    how = 'refused'\n"
    "except Exception as e:\n"
    "    how = 'driver:' + type(e).__name__\n"
    "print(','.join(kept) + '|' + how)\n"
)


def _probe(extra_env):
    env = {k: v for k, v in os.environ.items() if k != "HERMES_ORACLE_LIVE_TESTS"}
    env.update(LEAKED)
    env.update(extra_env)
    proc = subprocess.run([sys.executable, "-c", PROBE], cwd=str(CHECKOUT), env=env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr
    kept, how = proc.stdout.strip().split("|")
    return [k for k in kept.split(",") if k], how


class TestNoDatabaseInUnitTests(unittest.TestCase):
    @unittest.skipIf(os.environ.get("HERMES_ORACLE_LIVE_TESTS") == "1", "live run: the driver is not guarded")
    def test_driver_connect_functions_refuse(self):
        import oracledb

        for fn in ("connect", "create_pool"):
            with self.assertRaisesRegex(AssertionError, "tried to connect to a database"):
                getattr(oracledb, fn)(user="u", password="p", dsn="127.0.0.1:1/none")

    @unittest.skipIf(os.environ.get("HERMES_ORACLE_LIVE_TESTS") == "1", "live run")
    def test_provider_with_credentials_does_not_get_a_pool(self):
        # A test that forgets to mock the pool ends with an init error, never
        # with a connection.
        from unittest.mock import patch

        settings = {"OCI_DB_USER": "u", "OCI_DB_PASSWORD": "p", "OCI_DB_DSN": "127.0.0.1:1/none"}
        p = oracle_plugin.OracleMemoryProvider()
        with patch.object(oracle_plugin, "get_secret", side_effect=lambda k, d=None: settings.get(k, d)):
            p.initialize("s")
        self.assertIsNone(p._pool)
        self.assertIn("tried to connect to a database", p._init_error)

    def test_inherited_database_settings_are_dropped(self):
        kept, how = _probe({})
        self.assertEqual(kept, [])
        self.assertEqual(how, "refused")

    def test_live_run_keeps_settings_and_the_real_driver(self):
        kept, how = _probe({"HERMES_ORACLE_LIVE_TESTS": "1"})
        self.assertEqual(kept, sorted(LEAKED))
        # The real driver ran: nothing listens on that port.
        self.assertTrue(how.startswith("driver:"), how)

    def test_other_values_do_not_enable_the_live_run(self):
        for value in ("0", "true", "yes", ""):
            kept, how = _probe({"HERMES_ORACLE_LIVE_TESTS": value})
            self.assertEqual((kept, how), ([], "refused"), value)


if __name__ == "__main__":
    unittest.main()
