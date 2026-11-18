#!/usr/bin/env python3
"""Smoke test for an installed Oracle memory provider.

Starts the provider with the OCI_DB_* settings from the environment (or from
--env-file) and checks that a memory can be written, read back, found in each
search mode, updated and deleted.

Every row the run writes carries a session id unique to that run, and the run
deletes them all before it exits, also when a check fails. It asserts nothing
about rows that were in the table before, so it holds on any database: an
empty one, or one that already stores memory.

    export OCI_DB_USER=... OCI_DB_PASSWORD=... OCI_DB_DSN=...
    python3 tests/verify_provider.py

    python3 tests/verify_provider.py --env-file ~/.hermes/.env
    python3 tests/verify_provider.py --table HERMES_VERIFY_SCRATCH

Exit status: 0 = every check passed, 1 = a check failed, 2 = could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
_REQUIRED_ENV = ("OCI_DB_USER", "OCI_DB_PASSWORD", "OCI_DB_DSN")
_SESSION_PREFIX = "verify-"


def _common() -> Any:
    """scripts/_common.py (settings file, host shims, plugin loader), by path."""
    import importlib.util

    name = "hermes_oracle_scripts_common"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / "_common.py")
        if spec is None or spec.loader is None:
            raise ImportError("scripts/_common.py is missing")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return module


def load_env(env_file: str | None) -> None:
    """Load KEY=VALUE settings from *env_file*, only when --env-file is given.
    Values already in the environment win."""
    _common().load_env(env_file)


def _load_plugin() -> Any:
    return _common().load_plugin()


class CheckFailed(Exception):
    pass


class Run:
    """One verification run: the provider, its unique session, and the report."""

    def __init__(self, provider: Any, session_id: str, text_wait: float, out: Any = None) -> None:
        self.p = provider
        self.sid = session_id
        self.text_wait = text_wait
        self.out = out or sys.stdout
        self.failed: list[str] = []
        self.warned: list[str] = []
        self.passed = 0

    # -- reporting -----------------------------------------------------------

    def _say(self, mark: str, label: str, detail: str = "") -> None:
        print(f"  [{mark}] {label}" + (f": {detail}" if detail else ""), file=self.out)

    def ok(self, label: str, detail: str = "") -> None:
        self.passed += 1
        self._say(" ok ", label, detail)

    def fail(self, label: str, detail: str = "") -> None:
        self.failed.append(label)
        self._say("FAIL", label, detail)

    def warn(self, label: str, detail: str = "") -> None:
        self.warned.append(label)
        self._say("warn", label, detail)

    def note(self, label: str, detail: str = "") -> None:
        """Something about the install worth knowing; never a failure."""
        self._say("note", label, detail)

    def check(self, label: str, cond: Any, detail: Any = "") -> bool:
        if cond:
            self.ok(label)
        else:
            self.fail(label, str(detail)[:400])
        return bool(cond)

    # -- tool calls ----------------------------------------------------------

    def call(self, tool: str, **args: Any) -> dict[str, Any]:
        """Run one memory tool and return its JSON object. A tool error comes
        back as {"error": ...}."""
        raw = self.p.handle_tool_call(tool, args)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {"error": str(raw)}
        return data if isinstance(data, dict) else {"error": str(raw)}

    def search_ids(self, query: str, mode: str) -> tuple[list[str], str]:
        """memory_ids of this run's rows that the search returns, and the
        error text when the search failed."""
        res = self.call("oracle_search", query=query, mode=mode, top_k=8, session_id=self.sid)
        if res.get("error"):
            return [], str(res["error"])
        return [r.get("memory_id") for r in res.get("results") or []], ""

    # -- checks --------------------------------------------------------------

    def run_checks(self) -> None:
        p = self.p
        marker = uuid.uuid4().hex[:12]
        fact = f"Verification memory {marker}: the provider stores and recalls this sentence."

        for w in p.health().get("schema_warnings") or []:
            self.note("schema", w)

        try:
            vec = p._embed("Oracle memory provider verification")
            detail = f"got {None if vec is None else len(vec)} dimensions"
        except Exception as e:  # noqa: BLE001
            vec, detail = None, str(e).splitlines()[0]
        if not self.check("in-database embedding returns a 768-dimension vector",
                          vec is not None and len(vec) == 768,
                          f"{detail}. Is the ONNX model {p._onnx_model} loaded in this schema?"):
            raise CheckFailed("no embedding")

        added = self.call("oracle_add", content=fact)
        mid = str(added.get("memory_id") or "")
        if not self.check("oracle_add stores a memory", bool(mid), added):
            raise CheckFailed("nothing stored")

        got = self.call("oracle_get", memory_id=mid)
        rows = got.get("results") or []
        self.check("oracle_get returns it unchanged", len(rows) == 1 and rows[0].get("content") == fact, got)
        self.check("the row carries this run's session id", bool(rows) and rows[0].get("session_id") == self.sid, got)

        batch = self.call("oracle_add", facts=[f"Verification batch {marker} first.", f"Verification batch {marker} second."])
        self.check("oracle_add stores a batch", batch.get("count") == 2 and len(batch.get("memory_ids") or []) == 2, batch)

        named = f"{_SESSION_PREFIX}{marker}:named"
        tag = f"verify:{marker}"
        up = self.call("oracle_add", memory_id=named, content=f"Verification named row {marker}.", tags=[tag])
        self.check("oracle_add with a memory_id and tags stores a named row", up.get("memory_id") == named, up)
        by_tag = self.call("oracle_get", tags=[tag])
        self.check("oracle_get finds it by tag", [r.get("memory_id") for r in by_tag.get("results") or []] == [named], by_tag)

        for mode in ("vector", "hybrid"):
            ids, err = self.search_ids(fact, mode)
            self.check(f"{mode} search finds the memory", mid in ids, err or f"returned {ids}")

        self._check_text_search(marker, mid)

        new_text = f"Verification memory {marker}: updated."
        upd = self.call("oracle_update", memory_id=mid, content=new_text)
        got = self.call("oracle_get", memory_id=mid)
        rows = got.get("results") or []
        self.check("oracle_update replaces the content",
                   upd.get("result") == "updated" and bool(rows) and rows[0].get("content") == new_text, (upd, got))

        recalled = p.prefetch(fact)
        self.check("prefetch returns a recall block", isinstance(recalled, str), type(recalled).__name__)

        gone = self.call("oracle_delete", memory_id=mid)
        got = self.call("oracle_get", memory_id=mid)
        self.check("oracle_delete removes the memory",
                   gone.get("result") == "deleted" and got.get("count") == 0, (gone, got))

    def _check_text_search(self, marker: str, mid: str) -> None:
        """The text index is maintained in the background, so a new row shows
        up in text search some seconds after the commit. An error is a failure
        (no usable text index); a row that is not visible yet is a warning."""
        deadline = time.monotonic() + self.text_wait
        err = ""
        while True:
            ids, err = self.search_ids(marker, "text")
            if mid in ids or err or time.monotonic() >= deadline:
                break
            time.sleep(1.0)
        if err:
            self.fail("text search finds the memory", err)
        elif mid in ids:
            self.ok("text search finds the memory")
        else:
            self.warn(
                "text search finds the memory",
                f"not visible after {self.text_wait:.0f}s; the text index syncs later than that "
                "(vector and hybrid search are not affected)",
            )

    # -- cleanup -------------------------------------------------------------

    def leftover(self) -> int:
        with self.p._pool.acquire() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {self.p._table} WHERE session_id = :sid", {"sid": self.sid})  # nosec B608
            return int(cur.fetchone()[0])

    def cleanup(self) -> None:
        """Delete every row of this run. The session id is unique to the run,
        so nothing else can match."""
        try:
            with self.p._pool.acquire() as conn, conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self.p._table} WHERE session_id = :sid", {"sid": self.sid})  # nosec B608
                conn.commit()
            left = self.leftover()
            self.check("the run's rows are deleted", left == 0, f"{left} rows left under session {self.sid}")
        except Exception as e:  # noqa: BLE001
            self.fail("the run's rows are deleted", f"{e}; remove them with oracle_delete(session_id=\"{self.sid}\")")


def main(argv: list[str] | None = None, out: Any = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--env-file", help="file with KEY=VALUE settings (OCI_DB_*); the environment wins")
    parser.add_argument("--table", help="memory table to use (sets HERMES_ORACLE_TABLE for this run)")
    parser.add_argument("--text-wait", type=float, default=30.0,
                        help="seconds to wait for a new row to appear in text search (default 30)")
    parser.add_argument("--strict", action="store_true",
                        help="fail when a new row is not in text search within --text-wait")
    args = parser.parse_args(argv)
    out = out or sys.stdout

    try:
        load_env(args.env_file)
    except OSError as e:
        print(f"cannot read --env-file: {e}", file=out)
        return 2
    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"missing settings: {', '.join(missing)}. Export them or pass --env-file.", file=out)
        return 2

    saved_table = os.environ.get("HERMES_ORACLE_TABLE")
    if args.table:
        os.environ["HERMES_ORACLE_TABLE"] = args.table
    plugin = _load_plugin()
    saved_get_secret = plugin.get_secret
    # Settings come from the environment prepared above, whatever secret store
    # the host process has.
    plugin.get_secret = lambda k, default=None: os.environ.get(k, default)
    session_id = f"{_SESSION_PREFIX}{uuid.uuid4().hex[:16]}"
    provider = plugin.OracleMemoryProvider()
    run: Run | None = None
    try:
        provider.initialize(session_id)
        if provider._pool is None:
            print(f"provider did not start: {provider._init_error or provider.unavailable_reason()}", file=out)
            return 1
        print(f"provider {plugin.__version__}, table {provider._table}, model {provider._onnx_model}, "
              f"session {session_id}", file=out)
        run = Run(provider, session_id, max(0.0, args.text_wait), out)
        try:
            run.run_checks()
        except CheckFailed:
            pass
        except Exception as e:  # noqa: BLE001
            run.fail("unexpected error", f"{type(e).__name__}: {e}")
        finally:
            run.cleanup()
    finally:
        try:
            provider.shutdown()
        finally:
            plugin.get_secret = saved_get_secret
            if args.table:
                if saved_table is None:
                    os.environ.pop("HERMES_ORACLE_TABLE", None)
                else:
                    os.environ["HERMES_ORACLE_TABLE"] = saved_table

    bad = len(run.failed) + (len(run.warned) if args.strict else 0)
    print(f"{run.passed} passed, {len(run.failed)} failed, {len(run.warned)} warnings"
          + (" (strict: warnings count as failures)" if args.strict and run.warned else ""), file=out)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
