#!/usr/bin/env python3
"""One-command health check for a Hermes Oracle memory deployment.

Reports, in order:
  1. the plugin version (from the VERSION file)
  2. whether the oracledb driver imports in this interpreter
  3. which OCI_DB_* settings are set (names only, never values)
  4. a SELECT 1 probe against the database, when the settings resolve
  5. the toolset gate and memory.memory_enabled, when a Hermes config is readable

Exit codes: 0 = healthy, 1 = a problem was found.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

try:  # helpers shared by the scripts (scripts/_common.py)
    from . import _common as common
except ImportError:  # run as a script, or imported with this directory on sys.path
    import _common as common  # type: ignore[no-redef]

load_env = common.load_env

_REQUIRED = ("OCI_DB_USER", "OCI_DB_PASSWORD", "OCI_DB_DSN")
_OPTIONAL = ("OCI_WALLET_LOCATION", "OCI_WALLET_PASSWORD", "TNS_ADMIN")
_ALL = _REQUIRED + _OPTIONAL + ("HERMES_ORACLE_TABLE",)


def _version() -> str:
    version_file = common.PLUGIN_ROOT / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return f"unreadable ({exc})"


def _driver_ok() -> tuple[bool, str]:
    try:
        import oracledb  # noqa: F401

        return True, "imports"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _probe() -> tuple[bool, str]:
    import oracledb

    try:
        conn = oracledb.connect(**common.connect_kwargs())
    except Exception as exc:  # noqa: BLE001
        return False, f"connect failed: {exc}"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM DUAL")
            cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        conn.close()
        return False, f"SELECT 1 failed: {exc}"
    conn.close()
    return True, "SELECT 1 ok"


def _hermes_gate() -> dict[str, str]:
    """The toolset gate and memory.memory_enabled, when the config is readable."""
    out: dict[str, str] = {}
    try:
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        config = load_config()
        platform_toolsets = config.get("platform_toolsets") or {}
        for platform in sorted(platform_toolsets):
            key = f"tools.{platform}"
            try:
                enabled = _get_platform_tools(config, platform, include_default_mcp_servers=True)
                out[key] = "has memory toolset" if "memory" in enabled else "memory toolset OFF"
            except Exception:  # noqa: BLE001
                out[key] = "unreadable"
        memory = config.get("memory") or {}
        out["memory.memory_enabled"] = "yes" if memory.get("memory_enabled", True) else "no"
    except Exception:  # noqa: BLE001
        out["hermes_config"] = "unreadable (not inside Hermes, or no config)"
    return out


def run(args: argparse.Namespace) -> int:
    load_env(getattr(args, "env_file", None))

    problems: list[str] = []
    print(f"version: {_version()}")

    driver, detail = _driver_ok()
    print(f"driver: {'ok' if driver else 'MISSING'} ({detail})")
    if not driver:
        problems.append("oracledb does not import in this interpreter")

    set_now = [name for name in _ALL if os.environ.get(name)]
    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    print(f"settings set: {', '.join(set_now) if set_now else '(none)'}")
    if missing:
        print(f"settings missing: {', '.join(missing)}")
        problems.append(f"missing required settings: {', '.join(missing)}")

    if not missing and driver:
        ok, msg = _probe()
        print(f"probe: {msg}")
        if not ok:
            problems.append("database probe failed")

    for key, value in _hermes_gate().items():
        print(f"{key}: {value}")

    if problems:
        for p in problems:
            print(f"PROBLEM: {p}", file=sys.stderr)
        return 1
    print("doctor: OK")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Check a Hermes Oracle memory deployment and report what is missing."
    )
    p.add_argument("--env-file", default=None,
                   help="load OCI_DB_* settings from this KEY=VALUE file (default: environment only)")
    args = p.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
