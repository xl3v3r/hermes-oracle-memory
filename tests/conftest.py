import sys
import types
from unittest.mock import MagicMock
from dataclasses import dataclass

agent_mock = MagicMock()
class MemoryProvider:
    pass

@dataclass
class RecallStatus:
    provider_label: str
    count: int

# We need to construct actual modules for python to import them
agent_mod = types.ModuleType('agent')
agent_memory_provider_mod = types.ModuleType('agent.memory_provider')
agent_memory_provider_mod.MemoryProvider = MemoryProvider
agent_memory_provider_mod.RecallStatus = RecallStatus
agent_memory_provider_mod.is_trivial_prompt = MagicMock(return_value=False)
agent_mod.memory_provider = agent_memory_provider_mod

agent_secret_scope_mod = types.ModuleType('agent.secret_scope')
agent_secret_scope_mod.get_secret = MagicMock(return_value=None)
agent_mod.secret_scope = agent_secret_scope_mod

sys.modules['agent'] = agent_mod
sys.modules['agent.memory_provider'] = agent_memory_provider_mod
sys.modules['agent.secret_scope'] = agent_secret_scope_mod

tools_mod = types.ModuleType('tools')
tools_registry_mod = types.ModuleType('tools.registry')
class _ErrorStr(str):
    def __eq__(self, other):
        if other == "error":
            return True
        return super().__eq__(other)

tools_registry_mod.tool_error = MagicMock(side_effect=lambda msg, *args, **kwargs: _ErrorStr(f"error: {msg}"))
tools_mod.registry = tools_registry_mod

sys.modules['tools'] = tools_mod
sys.modules['tools.registry'] = tools_registry_mod

if 'oracledb' not in sys.modules:
    try:
        import oracledb
    except ImportError:
        sys.modules['oracledb'] = MagicMock()

if 'oci' not in sys.modules:
    try:
        import oci
    except ImportError:
        sys.modules['oci'] = MagicMock()



# ---------------------------------------------------------------------------
# An inherited HERMES_HOME is dropped for the whole run. The tests build their
# homes in the temp dir (patching Path.home or setting HERMES_HOME themselves),
# and a HERMES_HOME exported by the calling shell (the Hermes Desktop shell
# does) would override them. The value is kept for the link guard below.
#
# pytest imports this file as "conftest" and the tests import it again as
# "tests.conftest", so the value lives in one shared module, not per copy.
# ---------------------------------------------------------------------------
import os as _os
import tempfile as _tempfile
from pathlib import Path as _Path

_run_state = sys.modules.setdefault("_hom_test_run_state", types.ModuleType("_hom_test_run_state"))
if not hasattr(_run_state, "inherited_hermes_home"):
    setattr(_run_state, "inherited_hermes_home", _os.environ.pop("HERMES_HOME", None) or None)
_INHERITED_HERMES_HOME = getattr(_run_state, "inherited_hermes_home")


# ---------------------------------------------------------------------------
# Runtime link guard: while the suite runs, os.symlink and os.link (and so
# Path.symlink_to / Path.hardlink_to, which call them) refuse a link whose
# location or target lies outside the temp dir. No test can link into the
# installed plugin or the checkout, however the call is spelled.
# ---------------------------------------------------------------------------


def _real_home():
    try:
        import pwd

        return _Path(pwd.getpwuid(_os.getuid()).pw_dir).resolve()
    except (ImportError, KeyError, AttributeError, OSError):
        return None


def check_link_paths(target, link, target_relative_to_link=True):
    """Raise AssertionError unless the link's directory and its target both
    resolve inside tempfile.gettempdir(), and that temp dir is a safe one:

    - It does not contain the user's real home. HOME may be faked, so the
      real home comes from the passwd entry.
    - It does not overlap an installed plugin dir, in either direction. The
      installed plugin dirs are ``.hermes/plugins`` under the real home, and
      ``$HERMES_HOME/plugins`` for the HERMES_HOME the run was started with
      (conftest drops that variable; ``_INHERITED_HERMES_HOME`` keeps it).
    - It does not lie inside ``$HERMES_HOME/plugins`` for a HERMES_HOME set
      during the run. That home is a test's fake, built in the temp dir, so
      the temp dir may contain it.
    """
    tmp = _Path(_tempfile.gettempdir()).resolve()
    home = _real_home()
    if home is not None and home.is_relative_to(tmp):
        raise AssertionError(
            f"the temp dir {tmp} contains the home dir {home}; set TMPDIR to a dedicated directory"
        )
    checks = [((home / ".hermes" / "plugins").resolve(), True)] if home is not None else []
    if _INHERITED_HERMES_HOME:
        checks.append(((_Path(_INHERITED_HERMES_HOME).expanduser() / "plugins").resolve(), True))
    if _os.environ.get("HERMES_HOME"):
        checks.append(((_Path(_os.environ["HERMES_HOME"]) / "plugins").resolve(), False))
    for plugins, both_ways in checks:
        if tmp.is_relative_to(plugins) or (both_ways and plugins.is_relative_to(tmp)):
            raise AssertionError(
                f"the temp dir {tmp} overlaps the installed plugin dir {plugins}; "
                "set TMPDIR to a dedicated directory"
            )
    link_dir = _Path(_os.path.abspath(_os.fsdecode(link))).parent.resolve()
    tgt = _Path(_os.fsdecode(target))
    if not tgt.is_absolute():
        tgt = (link_dir if target_relative_to_link else _Path.cwd()) / tgt
    for p in (link_dir, tgt.resolve()):
        if not p.is_relative_to(tmp):
            raise AssertionError(f"test tried to create a link outside the temp dir: {link} -> {target}")


def _install_link_guard():
    if getattr(_os.symlink, "_hom_link_guard", False):
        return  # conftest imported a second time (as tests.conftest)
    real_symlink, real_link = _os.symlink, _os.link

    def symlink(src, dst, target_is_directory=False, *, dir_fd=None):
        if dir_fd is not None:
            raise AssertionError("tests may not create links relative to a dir_fd")
        check_link_paths(src, dst)
        return real_symlink(src, dst, target_is_directory)

    def link(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
        if src_dir_fd is not None or dst_dir_fd is not None:
            raise AssertionError("tests may not create links relative to a dir_fd")
        check_link_paths(src, dst, target_relative_to_link=False)
        if follow_symlinks:
            return real_link(src, dst)
        return real_link(src, dst, follow_symlinks=False)

    symlink._hom_link_guard = True  # type: ignore[attr-defined]
    link._hom_link_guard = True  # type: ignore[attr-defined]
    _os.symlink, _os.link = symlink, link
    # Python 3.10's pathlib keeps its own references to os.symlink / os.link
    # (Path.symlink_to / hardlink_to call them through _NormalAccessor).
    import pathlib as _pathlib

    accessor = getattr(_pathlib, "_NormalAccessor", None)
    if accessor is not None:
        if hasattr(accessor, "symlink"):
            accessor.symlink = staticmethod(symlink)
        if hasattr(accessor, "link"):
            accessor.link = staticmethod(link)


_install_link_guard()
