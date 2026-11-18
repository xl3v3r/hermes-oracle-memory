"""The helper scripts (#26, #32, #24): each prints its help on a machine that
has neither Hermes nor the optional packages, and they share one set of
helpers (scripts/_common.py). No database."""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import tests.conftest  # noqa: E402,F401
from scripts import _common as common  # noqa: E402

REPO = Path(root_dir)
SCRIPTS = sorted(p for p in (REPO / "scripts").glob("*.py") if not p.name.startswith("_"))
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}


class TestEveryScriptPrintsHelp(unittest.TestCase):
    def test_there_are_scripts(self):
        self.assertGreaterEqual(len(SCRIPTS), 6)

    def test_help_works_without_hermes_or_optional_packages(self):
        # No `agent` package, no onnx2oracle, no credentials, an empty HOME.
        with tempfile.TemporaryDirectory() as home:
            for script in SCRIPTS:
                proc = subprocess.run(
                    [sys.executable, str(script), "--help"], cwd=home, env=dict(CLEAN_ENV, HOME=home),
                    capture_output=True, text=True, timeout=120, check=False,
                )
                self.assertEqual(proc.returncode, 0, f"{script.name}: {proc.stderr[-400:]}")
                self.assertIn("usage:", proc.stdout, script.name)
                self.assertIn("--env-file", proc.stdout, script.name)

    def test_no_script_imports_an_optional_package_at_module_level(self):
        import ast

        optional = {"onnx2oracle", "onnx", "onnxruntime", "transformers", "oracledb", "oci", "agent", "tools"}
        for script in SCRIPTS:
            tree = ast.parse(script.read_text(encoding="utf-8"))
            for node in tree.body:
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [(node.module or "").split(".")[0]]
                self.assertFalse(optional & set(names), f"{script.name}: module-level import of {names}")

    def test_setup_onnx_model_names_the_missing_prerequisite(self):
        from scripts import setup_onnx_model

        err = io.StringIO()
        with patch.dict(sys.modules, {"onnx2oracle": None, "onnx2oracle.pipeline": None, "onnx2oracle.presets": None}), \
                patch.object(setup_onnx_model, "load_model", side_effect=AssertionError("must not connect")), \
                redirect_stderr(err):
            self.assertEqual(setup_onnx_model.main([]), 2)
        self.assertIn("python -m pip install onnx2oracle", err.getvalue())
        self.assertIn("github.com/jasperan/onnx2oracle", err.getvalue())

    def test_onnx2oracle_is_documented_and_never_vendored(self):
        install = (REPO / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("https://github.com/jasperan/onnx2oracle", install)
        pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(pyproject, r'(?m)^onnx = \[\s*"onnx2oracle')
        self.assertFalse(list(REPO.glob("**/onnx2oracle/**/*.py")))


class TestSharedHelpers(unittest.TestCase):
    def test_scripts_do_not_define_their_own_copies(self):
        for script in SCRIPTS:
            src = script.read_text(encoding="utf-8")
            self.assertNotIn("def load_env", src, script.name)
            self.assertNotIn('".oci" / "wallet"', src, script.name)
            self.assertNotIn('types.ModuleType("agent")', src, script.name)

    def test_no_script_sends_text_to_an_external_embedding_api(self):
        for script in SCRIPTS:
            src = script.read_text(encoding="utf-8").lower()
            for needle in ("openrouter", "urllib.request", "requests.post", "api.openai.com"):
                self.assertNotIn(needle, src, f"{script.name}: {needle}")

    def test_load_env_requires_a_path_and_keeps_the_environment(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HOM_A": "env"}, clear=True):
            common.load_env(None)
            env = Path(td) / "e"
            env.write_text("export HOM_A='file'\nHOM_B=\"b\"\n# comment\nnot a setting\n")
            common.load_env(str(env))
            self.assertEqual((os.environ["HOM_A"], os.environ["HOM_B"]), ("env", "b"))

    def test_hermes_home_order(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(sys.modules, {"hermes_constants": None}), patch("pathlib.Path.home", return_value=Path(td)):
                with patch.dict(os.environ, {"HERMES_HOME": ""}):
                    self.assertEqual(common.hermes_home(), Path(td) / ".hermes")
                with patch.dict(os.environ, {"HERMES_HOME": str(Path(td) / "profile")}):
                    self.assertEqual(common.hermes_home(), Path(td) / "profile")
            host = MagicMock()
            host.get_hermes_home.return_value = str(Path(td) / "from-host")
            with patch.dict(sys.modules, {"hermes_constants": host}), patch.dict(os.environ, {"HERMES_HOME": "/ignored"}):
                self.assertEqual(common.hermes_home(), Path(td) / "from-host")

    def test_wallet_is_found_under_the_active_hermes_home(self):
        # #24: the lookup used ~/.hermes/oracle_wallet whatever the home was.
        with tempfile.TemporaryDirectory() as td:
            home, profile = Path(td) / "home", Path(td) / "profile"
            (profile / "oracle_wallet").mkdir(parents=True)
            (profile / "oracle_wallet" / "cwallet.sso").write_text("x")
            home.mkdir()
            with patch("pathlib.Path.home", return_value=home), patch.dict(sys.modules, {"hermes_constants": None}):
                with patch.dict(os.environ, {"HERMES_HOME": str(profile)}, clear=True):
                    self.assertEqual(common.wallet_dir(), str(profile / "oracle_wallet"))
                with patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(common.wallet_dir(), "")
                with patch.dict(os.environ, {"HERMES_HOME": str(profile), "TNS_ADMIN": "/t"}, clear=True):
                    self.assertEqual(common.wallet_dir(), "/t")
                with patch.dict(os.environ, {"TNS_ADMIN": "/t", "OCI_WALLET_LOCATION": "/w"}, clear=True):
                    self.assertEqual(common.wallet_dir(), "/w")

    def test_connect_kwargs(self):
        with patch.dict(os.environ, {"OCI_DB_USER": "u"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Missing OCI_DB_PASSWORD / OCI_DB_DSN"):
                common.connect_kwargs()
        env = {"OCI_DB_USER": "u", "OCI_DB_PASSWORD": "p", "OCI_DB_DSN": "d"}
        with patch.dict(os.environ, env, clear=True), patch.object(common, "wallet_dir", return_value=""):
            self.assertEqual(common.connect_kwargs(), {"user": "u", "password": "p", "dsn": "d"})
        with patch.dict(os.environ, dict(env, OCI_WALLET_PASSWORD="wp"), clear=True), \
                patch.object(common, "wallet_dir", return_value="/w"):
            self.assertEqual(common.connect_kwargs(), {
                "user": "u", "password": "p", "dsn": "d",
                "config_dir": "/w", "wallet_location": "/w", "wallet_password": "wp",
            })

    def test_host_shims_never_replace_what_is_there(self):
        before = {k: sys.modules.get(k) for k in ("agent", "agent.memory_provider", "agent.secret_scope",
                                                    "tools", "tools.registry")}
        common.install_host_shims()
        for name, module in before.items():
            self.assertIs(sys.modules.get(name), module, name)

    def test_host_shims_in_a_bare_interpreter(self):
        code = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(REPO / 'scripts')!r})\n"
            "import _common\n"
            "_common.install_host_shims()\n"
            "from agent.secret_scope import get_secret\n"
            "from tools.registry import tool_error\n"
            "import os; os.environ['X_SET'] = 'v'\n"
            "print(get_secret('X_SET'), get_secret('X_UNSET', 'dflt'), json.loads(tool_error('boom'))['error'])\n"
        )
        with tempfile.TemporaryDirectory() as home:
            proc = subprocess.run([sys.executable, "-c", code], env=dict(CLEAN_ENV, HOME=home), cwd=home,
                                  capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "v dflt boom")

    def test_host_shims_work_when_the_helper_is_loaded_under_an_unregistered_name(self):
        # tests/check_wheel.py loads the installed helper by path without
        # putting it in sys.modules; a @dataclass inside the helper failed there.
        code = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('anon_helper', {str(REPO / 'scripts' / '_common.py')!r})\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "m.install_host_shims()\n"
            "from agent.memory_provider import RecallStatus\n"
            "r = RecallStatus(provider_label='x', count=2)\n"
            "print(r.provider_label, r.count, 'anon_helper' in sys.modules)\n"
        )
        with tempfile.TemporaryDirectory() as home:
            proc = subprocess.run([sys.executable, "-c", code], env=dict(CLEAN_ENV, HOME=home), cwd=home,
                                  capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr[-600:])
        self.assertEqual(proc.stdout.strip(), "x 2 False")

    def test_sync_skills_loads_the_helpers_by_path(self):
        from scripts import sync_skills

        mod = sync_skills._load_common()
        self.assertTrue(str(mod.__file__).endswith(os.path.join("scripts", "_common.py")))
        self.assertIs(sync_skills._load_common(), mod)
        self.assertEqual(sync_skills.hermes_home(), mod.hermes_home())

    def test_interpreter_search_follows_a_relocated_home(self):
        # #24: only ~/.hermes/installs was searched, with a POSIX-only layout.
        from scripts import sync_skills

        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "srv" / "hermes"
            py = home / "installs" / "i1" / "environments" / "e1" / "venv" / "bin" / "python"
            py.parent.mkdir(parents=True)
            py.write_text("")
            probed = []

            def run(cmd, **kw):
                probed.append(cmd[0])
                return MagicMock(returncode=1)

            with patch.dict(sys.modules, {"oracledb": None}), \
                    patch.dict(os.environ, {"HERMES_HOME": str(home)}), \
                    patch("pathlib.Path.home", return_value=Path(td) / "nohome"), \
                    patch("subprocess.run", side_effect=run), \
                    patch("os.execve", side_effect=AssertionError("must not re-exec")):
                os.environ.pop("_HERMES_ORACLE_SYNC_REEXEC", None)
                sync_skills._reexec_with_oracle_driver()
            self.assertEqual(probed, [str(py)])


if __name__ == "__main__":
    unittest.main()
