#!/usr/bin/env python3
"""Shell access to the Oracle memory store, outside a Hermes session.

Runs the same handlers the oracle_* tools use, through the plugin's provider,
so an operator can add, search, get and delete rows from a shell. Credentials
come from the environment, or from --env-file.

    python3 scripts/memory_cli.py add "fact" [--memory-id ID] [--tags a,b]
    python3 scripts/memory_cli.py search "query" [--top-k 8] [--mode hybrid]
    python3 scripts/memory_cli.py get --memory-id ID [--tags a,b]
    python3 scripts/memory_cli.py get --memory-id ID --delete

The reply is the tool's JSON on stdout. Exit 0 on success, 1 when the reply is
a tool error, 2 on bad usage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

try:  # helpers shared by the scripts (scripts/_common.py)
    from . import _common as common
except ImportError:  # run as a script, or imported with this directory on sys.path
    import _common as common  # type: ignore[no-redef]

load_env = common.load_env


def _split_tags(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    tags = [t.strip() for t in raw.split(",") if t.strip()]
    return tags or None


def _provider(table: str | None) -> Any:
    plugin = common.load_plugin()
    if table:
        os.environ["HERMES_ORACLE_TABLE"] = table
    provider = plugin.OracleMemoryProvider()
    provider.initialize("memory-cli", hermes_home=common.hermes_home())
    return provider


def run(args: argparse.Namespace) -> int:
    load_env(getattr(args, "env_file", None))
    provider = _provider(args.table)
    try:
        if args.command == "add":
            payload: dict[str, Any] = {"content": args.content}
            if args.memory_id:
                payload["memory_id"] = args.memory_id
            tags = _split_tags(args.tags)
            if tags:
                payload["tags"] = tags
            if args.target:
                payload["target"] = args.target
            out = provider.handle_tool_call("oracle_add", payload)
        elif args.command == "search":
            payload = {"query": args.query}
            if args.top_k is not None:
                payload["top_k"] = args.top_k
            if args.mode:
                payload["mode"] = args.mode
            out = provider.handle_tool_call("oracle_search", payload)
        elif args.command == "get":
            if args.delete:
                if not args.memory_id:
                    print("--delete needs --memory-id", file=sys.stderr)
                    return 2
                out = provider.handle_tool_call("oracle_delete", {"memory_id": args.memory_id})
            else:
                payload = {}
                if args.memory_id:
                    payload["memory_id"] = args.memory_id
                tags = _split_tags(args.tags)
                if tags:
                    payload["tags"] = tags
                if args.limit:
                    payload["limit"] = args.limit
                out = provider.handle_tool_call("oracle_get", payload)
        else:
            print(f"unknown command: {args.command}", file=sys.stderr)
            return 2
        print(out)
        try:
            reply = json.loads(out)
        except ValueError:
            return 0
        return 1 if isinstance(reply, dict) and "error" in reply else 0
    finally:
        provider.shutdown()


def main() -> None:
    p = argparse.ArgumentParser(
        description="Add, search, get and delete Oracle memory rows from a shell."
    )
    p.add_argument("--env-file", default=None,
                   help="load OCI_DB_* settings from this KEY=VALUE file (default: environment only)")
    p.add_argument("--table", default=None, help="memory table name (default: HERMES_ORACLE_TABLE)")
    sub = p.add_subparsers(dest="command", required=True)

    add_p = sub.add_parser("add", help="store a fact")
    add_p.add_argument("content", help="the fact text")
    add_p.add_argument("--memory-id", default=None, help="stable id (upsert)")
    add_p.add_argument("--tags", default=None, help="comma-separated tags")
    add_p.add_argument("--target", default=None, help="memory or user (default memory)")

    search_p = sub.add_parser("search", help="hybrid / vector / text search")
    search_p.add_argument("query", help="what to search for")
    search_p.add_argument("--top-k", type=int, default=None, help="max results (default 8)")
    search_p.add_argument("--mode", default=None, help="hybrid, vector or text (default hybrid)")

    get_p = sub.add_parser("get", help="read rows by id and/or tags")
    get_p.add_argument("--memory-id", default=None, help="exact id")
    get_p.add_argument("--tags", default=None, help="comma-separated tags")
    get_p.add_argument("--limit", type=int, default=None, help="max rows (default 10)")
    get_p.add_argument("--delete", action="store_true", help="delete the --memory-id row instead")

    args = p.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
