#!/usr/bin/env python3
"""Check a built wheel and its installation (#25).

Run it with the interpreter of an environment the wheel was installed into,
from a directory outside the checkout, with the wheel file as its argument:

    python -m build
    python -m venv /tmp/wheel-venv && /tmp/wheel-venv/bin/python -m pip install dist/*.whl
    cd /tmp && /tmp/wheel-venv/bin/python <checkout>/tests/check_wheel.py <checkout>/dist/*.whl

It verifies that the wheel carries what the provider loads at run time
(scripts/sync_skills.py and its helper, the bundled skills, plugin.yaml),
that it carries no compiled files, and that the installed package can load
its skill sync module.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import sys
import zipfile
from pathlib import Path

PACKAGE = "hermes_oracle_memory"
REQUIRED = (
    "__init__.py",
    "plugin.yaml",
    "scripts/_common.py",
    "scripts/sync_skills.py",
    "skills/oracle-import/SKILL.md",
    "skills/oracle-skill-corpus/SKILL.md",
)


def check_archive(wheel: Path) -> list[str]:
    """Problems with the wheel file itself."""
    names = zipfile.ZipFile(wheel).namelist()
    problems = [f"missing from the wheel: {PACKAGE}/{f}" for f in REQUIRED if f"{PACKAGE}/{f}" not in names]
    problems += [f"compiled file in the wheel: {n}" for n in names if "__pycache__" in n or n.endswith((".pyc", ".pyo"))]
    problems += [f"test file in the wheel: {n}" for n in names if n.startswith(f"{PACKAGE}/tests/")]
    return problems


def check_installed() -> list[str]:
    """Problems with the installed package: it must load its own skill sync
    module, from its own directory."""
    problems: list[str] = []
    dist = importlib.metadata.distribution("hermes-oracle-memory")
    package_dir = Path(str(dist.locate_file(PACKAGE))).resolve()
    here = Path.cwd().resolve()
    if here == package_dir.parent or (here / "plugin.yaml").exists():
        return ["run this check from a directory outside the checkout"]

    # Outside Hermes the plugin's imports need the host shims, which ship in
    # the package (scripts/_common.py).
    helper = package_dir / "scripts" / "_common.py"
    spec = importlib.util.spec_from_file_location("installed_scripts_common", helper) if helper.is_file() else None
    if spec is None or spec.loader is None:
        return [f"not installed: {helper}"]
    common = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(common)
    common.install_host_shims()

    plugin = importlib.import_module(PACKAGE)
    if Path(str(plugin.__file__)).resolve().parent != package_dir:
        problems.append(f"imported {plugin.__file__}, expected the package in {package_dir}")
    if plugin.__version__ != dist.version:
        problems.append(f"__version__ {plugin.__version__} != distribution version {dist.version}")
    manifest = (package_dir / "plugin.yaml").read_text(encoding="utf-8")
    if f"version: {dist.version}\n" not in manifest:
        problems.append(f"plugin.yaml does not carry version {dist.version}")

    sync = plugin._sync_skills_module()
    if Path(str(sync.__file__)).resolve() != package_dir / "scripts" / "sync_skills.py":
        problems.append(f"skill sync module loaded from {sync.__file__}")
    for name in ("parse_skill_file", "sync_skills_library", "default_skill_roots"):
        if not callable(getattr(sync, name, None)):
            problems.append(f"skill sync module has no {name}()")

    registered: dict[str, Path] = {}

    class Context:
        def register_memory_provider(self, provider):
            self.provider = provider

        def register_skill(self, name, path, *args, **kwargs):
            registered[name] = Path(path)

        def register_hook(self, *args, **kwargs):
            pass

    plugin.register(Context())
    for name in ("oracle-import", "oracle-skill-corpus"):
        if name not in registered:
            problems.append(f"bundled skill {name} was not registered")
        elif not registered[name].exists():
            problems.append(f"bundled skill {name} points at a missing file: {registered[name]}")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print((__doc__ or "").strip(), file=sys.stderr)
        return 2
    wheel = Path(args[0])
    problems = check_archive(wheel) + check_installed()
    for p in problems:
        print(f"FAIL: {p}")
    if not problems:
        dist = importlib.metadata.distribution("hermes-oracle-memory")
        print(f"ok: {wheel.name} installs as {PACKAGE} {dist.version} and loads its skill sync module")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
