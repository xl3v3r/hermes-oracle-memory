"""Helpers shared by the scripts in this directory.

- load_env:       KEY=VALUE settings from a file the user named with --env-file.
- hermes_home:    the active Hermes home (profile-aware inside Hermes).
- wallet_dir:     where the Oracle wallet is, if there is one.
- connect_kwargs: arguments for oracledb.connect() from the OCI_* settings.
- load_plugin:    the provider module, also outside a Hermes process.

A script loads this module by path (see load_common in each script), so it
works when the script is run directly, imported as scripts.<name>, or loaded
by the plugin under another module name.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_MODULE = "hermes_oracle_memory_plugin"


def load_env(env_file: str | os.PathLike[str] | None) -> None:
    """Load KEY=VALUE settings from *env_file*, only when the user passed
    --env-file. Values already in the environment win. No script reads a
    credentials file on its own."""
    if not env_file:
        return
    for line in Path(env_file).expanduser().read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def hermes_home() -> Path:
    """The active Hermes home. Inside Hermes this honours the profile
    override; standalone it is $HERMES_HOME, else ~/.hermes."""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        pass
    env = (os.environ.get("HERMES_HOME") or "").strip()
    return Path(os.path.expanduser(env)) if env else Path.home() / ".hermes"


def wallet_dir(home: str | os.PathLike[str] | None = None) -> str:
    """The wallet directory: OCI_WALLET_LOCATION, then TNS_ADMIN, then the
    first of ~/.oci/wallet and <Hermes home>/oracle_wallet that holds a
    wallet. "" when there is none (a plain TCP connection)."""
    for name in ("OCI_WALLET_LOCATION", "TNS_ADMIN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    base = Path(home) if home else hermes_home()
    for candidate in (Path.home() / ".oci" / "wallet", base / "oracle_wallet"):
        try:
            if (candidate / "cwallet.sso").exists() or (candidate / "tnsnames.ora").exists():
                return str(candidate)
        except OSError:
            continue
    return ""


def connect_kwargs() -> dict[str, Any]:
    """Arguments for oracledb.connect() / create_pool() from OCI_DB_USER,
    OCI_DB_PASSWORD, OCI_DB_DSN and the wallet settings. Raises RuntimeError
    naming the settings that are missing."""
    missing = [k for k in ("OCI_DB_USER", "OCI_DB_PASSWORD", "OCI_DB_DSN") if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing {' / '.join(missing)}: export them or pass --env-file")
    kwargs: dict[str, Any] = {
        "user": os.environ["OCI_DB_USER"],
        "password": os.environ["OCI_DB_PASSWORD"],
        "dsn": os.environ["OCI_DB_DSN"],
    }
    wallet = wallet_dir()
    if wallet:
        kwargs["config_dir"] = wallet
        kwargs["wallet_location"] = wallet
        password = os.environ.get("OCI_WALLET_PASSWORD")
        if password:
            kwargs["wallet_password"] = password
    return kwargs


def install_host_shims() -> None:
    """Outside Hermes there is no `agent` or `tools` package. Provide the few
    names the plugin imports, backed by the process environment. Inside
    Hermes (or a test that already provides them) nothing is replaced."""
    try:
        import agent.memory_provider  # noqa: F401
        import agent.secret_scope  # noqa: F401
    except ImportError:
        agent_mod = types.ModuleType("agent")
        mp_mod = types.ModuleType("agent.memory_provider")

        class MemoryProvider:
            pass

        class RecallStatus:
            def __init__(self, provider_label: str, count: int) -> None:
                self.provider_label = provider_label
                self.count = count

        setattr(mp_mod, "MemoryProvider", MemoryProvider)
        setattr(mp_mod, "RecallStatus", RecallStatus)
        setattr(mp_mod, "is_trivial_prompt", lambda prompt: False)
        sec_mod = types.ModuleType("agent.secret_scope")
        setattr(sec_mod, "get_secret", lambda k, default=None: os.environ.get(k, default))
        setattr(agent_mod, "memory_provider", mp_mod)
        setattr(agent_mod, "secret_scope", sec_mod)
        sys.modules.update({"agent": agent_mod, "agent.memory_provider": mp_mod, "agent.secret_scope": sec_mod})
    try:
        import tools.registry  # noqa: F401
    except ImportError:
        import json

        tools_mod = types.ModuleType("tools")
        reg_mod = types.ModuleType("tools.registry")
        setattr(reg_mod, "tool_error", lambda msg, *a, **k: json.dumps({"error": str(msg)}))
        setattr(tools_mod, "registry", reg_mod)
        sys.modules.update({"tools": tools_mod, "tools.registry": reg_mod})


def load_plugin() -> Any:
    """The provider module (the plugin's __init__.py), loaded once by path."""
    cached = sys.modules.get(_PLUGIN_MODULE)
    if cached is not None:
        return cached
    install_host_shims()
    spec = importlib.util.spec_from_file_location(_PLUGIN_MODULE, PLUGIN_ROOT / "__init__.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the plugin from {PLUGIN_ROOT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PLUGIN_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_PLUGIN_MODULE, None)
        raise
    return module
