import os
import unittest
from pathlib import Path


class TestProviderLive(unittest.TestCase):
    def test_verify_provider_live(self):
        env_file = Path.home() / ".hermes" / ".env"
        if not env_file.exists():
            self.skipTest("~/.hermes/.env not found")
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        import sys
        import __init__ as oracle_plugin
        oracle_plugin.get_secret = lambda k, default=None: os.environ.get(k, default)
        try:
            probe = oracle_plugin.OracleMemoryProvider()
            probe.initialize("probe-live-availability")
            if not probe.is_available():
                reason = probe.unavailable_reason()
                probe.shutdown()
                self.skipTest(f"Oracle ADB unavailable: {reason}")
            probe.shutdown()
        except Exception as e:
            self.skipTest(f"Oracle ADB connection failed: {e}")

        import importlib.util
        verify_path = Path(__file__).resolve().parent / "verify_provider.py"
        spec = importlib.util.spec_from_file_location("verify_provider", verify_path)
        if spec is None or spec.loader is None:
            self.fail(f"could not load {verify_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            module.main()
        except SystemExit as se:
            if se.code != 0:
                self.fail(f"verify_provider exited with code {se.code}")


if __name__ == "__main__":
    unittest.main()
