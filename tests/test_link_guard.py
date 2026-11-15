"""Runtime link guard (#14): while the suite runs, os.symlink and os.link (and
so Path.symlink_to / Path.hardlink_to, which call them) refuse any link whose
location or target lies outside the temp dir, so no test can link into the
installed plugin or the checkout. Installed by tests/conftest.py."""

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest as guard  # noqa: E402

CHECKOUT = Path(__file__).resolve().parent.parent
_CHECKOUT_IN_TMP = CHECKOUT.is_relative_to(Path(tempfile.gettempdir()).resolve())


@unittest.skipIf(_CHECKOUT_IN_TMP, "the checkout lies in the temp dir, so it is no outside target")
class TestRuntimeLinkGuard(unittest.TestCase):
    def test_guard_installed(self):
        self.assertTrue(getattr(os.symlink, "_hom_link_guard", False))
        self.assertTrue(getattr(os.link, "_hom_link_guard", False))

    def test_symlink_to_outside_target_refused(self):
        with tempfile.TemporaryDirectory() as td:
            link = Path(td) / "x"
            with self.assertRaises(AssertionError):
                os.symlink(str(CHECKOUT), str(link))
            self.assertFalse(os.path.lexists(link))

    def test_relative_target_escaping_temp_dir_refused(self):
        with tempfile.TemporaryDirectory() as td:
            link = Path(td) / "x"
            up = os.path.relpath(CHECKOUT, td)
            with self.assertRaises(AssertionError):
                os.symlink(up, str(link))
            self.assertFalse(os.path.lexists(link))

    def test_link_location_outside_temp_dir_refused(self):
        with tempfile.TemporaryDirectory() as td:
            # /dev/null is not a directory: without the guard this fails with
            # an OSError instead, and nothing can be created there either way.
            with self.assertRaises(AssertionError):
                os.symlink(td, "/dev/null/hom-link")

    def test_path_methods_guarded(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(AssertionError):
                (Path(td) / "s").symlink_to(CHECKOUT)
            with self.assertRaises(AssertionError):
                (Path(td) / "h").hardlink_to(CHECKOUT / "VERSION")
            self.assertEqual(sorted(os.listdir(td)), [])

    def test_hardlink_to_outside_file_refused(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(AssertionError):
                os.link(str(CHECKOUT / "VERSION"), os.path.join(td, "v"))

    def test_links_inside_temp_dir_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "t"
            target.mkdir()
            (target / "f").write_text("x", encoding="utf-8")
            os.symlink(str(target), os.path.join(td, "s"))
            os.symlink("t", os.path.join(td, "rel"))
            os.link(str(target / "f"), os.path.join(td, "h"))
            self.assertTrue((Path(td) / "s" / "f").is_file())
            self.assertTrue((Path(td) / "rel" / "f").is_file())
            self.assertTrue((Path(td) / "h").is_file())

    def test_temp_dir_containing_home_refuses_every_link(self):
        try:
            import pwd

            real_home = pwd.getpwuid(os.getuid()).pw_dir
        except (ImportError, KeyError):
            self.skipTest("no passwd entry")
        with tempfile.TemporaryDirectory() as td:
            with unittest.mock.patch("tempfile.gettempdir", return_value=os.path.dirname(real_home) or "/"):
                with self.assertRaisesRegex(AssertionError, "contains the home"):
                    guard.check_link_paths(os.path.join(td, "t"), os.path.join(td, "l"))

    def test_temp_dir_overlapping_plugin_dir_refuses_every_link(self):
        try:
            import pwd

            hermes = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".hermes")
        except (ImportError, KeyError):
            self.skipTest("no passwd entry")
        with tempfile.TemporaryDirectory() as td:
            for tmp in (hermes, os.path.join(hermes, "plugins", "oracle")):
                with unittest.mock.patch("tempfile.gettempdir", return_value=tmp):
                    with self.assertRaisesRegex(AssertionError, "plugin"):
                        guard.check_link_paths(os.path.join(td, "t"), os.path.join(td, "l"))

    def test_plugin_dir_compared_after_resolving_links(self):
        from tests.test_skill_ownership import _link_in_tmp

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home = root / "home"
            (home / ".hermes").mkdir(parents=True)
            (root / "elsewhere").mkdir()
            _link_in_tmp(home / ".hermes" / "plugins", root / "elsewhere")
            with unittest.mock.patch.object(guard, "_real_home", return_value=home), \
                    unittest.mock.patch("tempfile.gettempdir", return_value=str(root / "elsewhere" / "x")):
                with self.assertRaisesRegex(AssertionError, "plugin"):
                    guard.check_link_paths(str(root / "t"), str(root / "l"))

    def test_hermes_home_plugin_dir_refuses_overlapping_temp_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            hh = root / "hh"
            with unittest.mock.patch.dict(os.environ, {"HERMES_HOME": str(hh)}), \
                    unittest.mock.patch("tempfile.gettempdir", return_value=str(hh / "plugins" / "x")):
                with self.assertRaisesRegex(AssertionError, "plugin"):
                    guard.check_link_paths(str(root / "t"), str(root / "l"))

    def test_fake_hermes_home_inside_temp_dir_allows_links(self):
        # Tests point HERMES_HOME at a fake home in the temp dir; its plugin
        # dir is a fake too, so links there stay allowed.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / "hh" / "plugins").mkdir(parents=True)
            with unittest.mock.patch.dict(os.environ, {"HERMES_HOME": str(root / "hh")}):
                guard.check_link_paths(str(root / "t"), str(root / "hh" / "plugins" / "l"))

    def test_pathlib_accessor_guarded_when_present(self):
        import pathlib

        acc = getattr(pathlib, "_normal_accessor", None)  # Python 3.10 keeps its own os.symlink / os.link
        if acc is None:
            self.skipTest("pathlib calls os.symlink / os.link directly")
        self.assertTrue(getattr(acc.symlink, "_hom_link_guard", False))
        self.assertTrue(getattr(acc.link, "_hom_link_guard", False))

    def test_check_is_exposed_for_helpers(self):
        with tempfile.TemporaryDirectory() as td:
            guard.check_link_paths(os.path.join(td, "t"), os.path.join(td, "l"))
            with self.assertRaises(AssertionError):
                guard.check_link_paths(str(CHECKOUT), os.path.join(td, "l"))


class TestStaticLinkScan(unittest.TestCase):
    """The source scan in test_suite_never_writes_outside_checkout."""

    def _scan(self, src, name="test_sample.py"):
        from tests.test_oracle import _link_violations

        return _link_violations(name, src)

    def test_aliased_and_shell_links_flagged(self):
        # Shell samples are split so the install scanner does not read these
        # strings as calls in this file.
        for src in (
            "from os import symlink\nsymlink(a, b)\n",
            "from os import link as mk\n",
            "os.link(a, b)\n",
            "p.hardlink_to(q)\n",
            "subprocess" ".run(['ln', '-s', a, b])\n",
            "os.sys" "tem('ln -s a b')\n",
            "os.symlink(a, b)\n",
            "p.symlink_to(q)\n",
            "cmd = 'ln -fs a b'\n",
            "cmd = 'ln --symbolic a b'\n",
            "cmd = ['/bin/ln', '-s', a, b]\n",
            "import os as o\no.symlink(a, b)\n",
            "import os as o\no.link(a, b)\n",
        ):
            self.assertTrue(self._scan(src), src)

    def test_helper_confinement_cannot_be_a_comment(self):
        helper = (
            "\ndef _link_in_tmp(link, target):\n"
            "    # gettempdir() relative_to(tmp)\n"
            "    os.symlink(target, link)\n"
        )
        self.assertTrue(self._scan(helper + "\nclass T:\n    pass\n"))

    def test_confined_helper_accepted(self):
        helper = (
            "\ndef _link_in_tmp(link, target):\n"
            "    tmp = Path(tempfile.gettempdir()).resolve()\n"
            "    for p in (link.parent, target):\n"
            "        Path(p).resolve().relative_to(tmp)\n"
            "    os.symlink(target, link)\n"
        )
        self.assertEqual(self._scan(helper + "\nclass T:\n    pass\n"), [])

    def test_plain_code_clean(self):
        for src in (
            "x = Path(a).read_text()\n",
            "self.assertTrue(p.is_symlink())\n",
            "os.unlink(p)\nos.readlink(p)\n",
            "n = math.log(x)  # ln of x\n",
        ):
            self.assertEqual(self._scan(src), [], src)


if __name__ == "__main__":
    unittest.main()
