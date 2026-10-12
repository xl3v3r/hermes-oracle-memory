#!/usr/bin/env python3
"""Verification script for OracleMemoryProvider with qwen/qwen3-embedding-4b."""

from __future__ import annotations

import json
import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path

# Load ~/.hermes/.env
env_file = Path.home() / ".hermes" / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k.strip(), v)

# Ensure agent and tools modules are present, providing shims if running standalone
if "agent" not in sys.modules:
    try:
        from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt
        from agent.secret_scope import get_secret
    except ImportError:
        agent_mod = types.ModuleType("agent")
        mp_mod = types.ModuleType("agent.memory_provider")

        class MemoryProvider:
            def initialize(self, session_id: str) -> None:
                pass
            def is_available(self) -> bool:
                return True
            def unavailable_reason(self) -> str:
                return ""
            def shutdown(self) -> None:
                pass

        @dataclass
        class RecallStatus:
            provider_label: str
            count: int

        mp_mod.MemoryProvider = MemoryProvider
        mp_mod.RecallStatus = RecallStatus
        mp_mod.is_trivial_prompt = lambda prompt: False
        agent_mod.memory_provider = mp_mod
        sys.modules["agent"] = agent_mod
        sys.modules["agent.memory_provider"] = mp_mod

        sec_mod = types.ModuleType("agent.secret_scope")
        sec_mod.get_secret = lambda k, default=None: os.environ.get(k, default)
        agent_mod.secret_scope = sec_mod
        sys.modules["agent.secret_scope"] = sec_mod

if "tools" not in sys.modules:
    try:
        from tools.registry import tool_error
    except ImportError:
        tools_mod = types.ModuleType("tools")
        reg_mod = types.ModuleType("tools.registry")
        reg_mod.tool_error = lambda msg: f"Error: {msg}"
        tools_mod.registry = reg_mod
        sys.modules["tools"] = tools_mod
        sys.modules["tools.registry"] = reg_mod

root_dir = str(Path(__file__).resolve().parent.parent)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from __init__ import OracleMemoryProvider, _EMBED_MODEL, _EMBED_DIM


def main() -> None:
    print(f"Configured model: {_EMBED_MODEL}")
    print(f"Configured dimensions: {_EMBED_DIM}")
    assert _EMBED_MODEL == "qwen/qwen3-embedding-4b", f"Unexpected model: {_EMBED_MODEL}"
    assert _EMBED_DIM == 768, f"Unexpected dim: {_EMBED_DIM}"

    provider = OracleMemoryProvider()
    print("Provider initialized instance")

    provider.initialize("test-verification-session")
    is_avail = provider.is_available()
    print(f"Is available: {is_avail}")
    if not is_avail:
        print(f"Unavailable reason: {provider.unavailable_reason()}")
        if provider._init_error:
            print(f"Init error: {provider._init_error}")
        sys.exit(1)

    print(f"Configured ONNX model: {provider._onnx_model or 'None (using OpenRouter 768-d fallback)'}")

    # 1. Verify single embedding generation
    print("Testing _embed (Option B in-database ONNX or Option C OpenRouter fallback)...")
    vec = provider._embed("Oracle 26ai Autonomous Database vector search verification")
    assert vec is not None, "Failed to get vector embedding"
    assert len(vec) == 768, f"Expected 768 dimensions, got {len(vec)}"
    print(f"Vector embedding verified: dim={len(vec)}, type={vec.typecode}")

    # 2. Verify batch embedding with edge cases (empty strings, whitespace, None)
    print("Testing _embed_openrouter with edge cases...")
    edge_vecs = provider._embed_openrouter(["valid query", "", "   ", None, "another query"])
    assert len(edge_vecs) == 5, f"Expected 5 results, got {len(edge_vecs)}"
    assert edge_vecs[0] is not None and len(edge_vecs[0]) == 768, "Failed valid query 1"
    assert edge_vecs[1] is None, "Empty string should yield None"
    assert edge_vecs[2] is None, "Whitespace string should yield None"
    assert edge_vecs[3] is None, "None should yield None"
    assert edge_vecs[4] is not None and len(edge_vecs[4]) == 768, "Failed valid query 2"
    print("Batch edge cases handled successfully.")

    # 3. Verify vector search
    print("Testing vector search...")
    vec_results = provider._search("database memory architecture", mode="vector", top_k=2)
    print(f"Vector search returned {len(vec_results)} results")
    if vec_results:
        print(f"Top vector match id: {vec_results[0].get('memory_id')}, score: {vec_results[0].get('score')}")

    # 4. Verify text search
    print("Testing text search...")
    text_results = provider._search("database memory architecture", mode="text", top_k=2)
    print(f"Text search returned {len(text_results)} results")
    if text_results:
        print(f"Top text match id: {text_results[0].get('memory_id')}, score: {text_results[0].get('score')}")

    # 5. Verify hybrid search
    print("Testing hybrid search...")
    hybrid_results = provider._search("database memory architecture", mode="hybrid", top_k=2)
    print(f"Hybrid search returned {len(hybrid_results)} results")
    if hybrid_results:
        print(f"Top hybrid match id: {hybrid_results[0].get('memory_id')}, score: {hybrid_results[0].get('score')}")

    # 6. Verify prefetch
    print("Testing prefetch...")
    prefetch_out = provider.prefetch("database memory architecture")
    print(f"Prefetch output length: {len(prefetch_out)} characters")

    # 7. Verify oracle_search tool call
    print("Testing handle_tool_call oracle_search...")
    tool_out = provider.handle_tool_call("oracle_search", {
        "query": "database memory architecture",
        "mode": "hybrid",
        "limit": 2,
    })
    print(f"Tool call output length: {len(tool_out)} characters")
    assert len(tool_out) > 0, "Empty tool call output"

    # 8. Verify live add and update tool calls via ONNX
    print("Testing handle_tool_call oracle_add with ONNX...")
    import uuid
    test_fact = f"Verification fact {uuid.uuid4().hex[:8]} for Oracle AI memory"
    add_out = provider.handle_tool_call("oracle_add", {
        "content": test_fact,
        "target": "memory",
    })
    print(f"oracle_add response: {add_out}")
    assert "Fact stored" in add_out or "memory_id" in add_out, "Failed to add fact"
    add_data = json.loads(add_out)
    new_mid = add_data.get("memory_id")

    if new_mid:
        print("Testing handle_tool_call oracle_update with ONNX...")
        upd_fact = f"Updated verification fact for {new_mid}"
        upd_out = provider.handle_tool_call("oracle_update", {
            "memory_id": new_mid,
            "content": upd_fact,
        })
        print(f"oracle_update response: {upd_out}")
        assert "updated" in upd_out.lower(), "Failed to update fact"

    # 9. Verify live fallback to OpenRouter when ONNX model is unavailable
    print("Testing live fallback from broken ONNX to OpenRouter 768-d...")
    orig_onnx = provider._onnx_model
    try:
        provider._onnx_model = "NONEXISTENT_ONNX_MODEL_TEST"
        fallback_results = provider._search("database memory architecture", mode="vector", top_k=2)
        print(f"Fallback vector search returned {len(fallback_results)} results")
        fallback_hybrid = provider._search("database memory architecture", mode="hybrid", top_k=2)
        print(f"Fallback hybrid search returned {len(fallback_hybrid)} results")
        fallback_add = provider._insert(
            memory_id=f"fallback_test_{uuid.uuid4().hex[:8]}",
            session_id="fallback-session",
            role="user",
            content="Fallback content test for OpenRouter",
            target="memory",
        )
        print("Fallback insert succeeded.")
    finally:
        provider._onnx_model = orig_onnx

    provider.shutdown()
    print("ALL VERIFICATION CHECKS PASSED!")


if __name__ == "__main__":
    main()
