import sys
import os
from unittest.mock import MagicMock

# Create mock objects for the dependencies
mock_agent = MagicMock()
mock_agent_memory_provider = MagicMock()
class MockMemoryProviderBase:
    def unavailable_reason(self):
        return ""
mock_agent_memory_provider.MemoryProvider = MockMemoryProviderBase
mock_agent_secret_scope = MagicMock()

mock_tools = MagicMock()
mock_tools_registry = MagicMock()

# Inject into sys.modules
sys.modules['agent'] = mock_agent
sys.modules['agent.memory_provider'] = mock_agent_memory_provider
sys.modules['agent.secret_scope'] = mock_agent_secret_scope
sys.modules['tools'] = mock_tools
sys.modules['tools.registry'] = mock_tools_registry

# Add root dir to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
import __init__ as plugin

def test_system_prompt_block_not_initialized_with_error():
    provider = plugin.OracleMemoryProvider()
    provider._pool = None
    provider._init_error = "mock error"

    result = provider.system_prompt_block()

    assert result == "# Oracle 26ai Memory\nUnavailable (mock error). Built-in MEMORY.md still applies."

def test_system_prompt_block_not_initialized_with_reason():
    provider = plugin.OracleMemoryProvider()
    provider._pool = None
    provider._init_error = ""
    # Override the default mock method
    provider.unavailable_reason = lambda: "mock reason"

    result = provider.system_prompt_block()

    assert result == "# Oracle 26ai Memory\nUnavailable (mock reason). Built-in MEMORY.md still applies."

def test_system_prompt_block_not_initialized_default():
    provider = plugin.OracleMemoryProvider()
    provider._pool = None
    provider._init_error = ""
    provider.unavailable_reason = lambda: ""

    result = provider.system_prompt_block()

    assert result == "# Oracle 26ai Memory\nUnavailable (not initialized). Built-in MEMORY.md still applies."

def test_system_prompt_block_initialized():
    provider = plugin.OracleMemoryProvider()
    provider._pool = "mock pool" # Any truthy value
    provider._agent_id = "mock_agent"

    result = provider.system_prompt_block()

    expected = (
        "# Oracle 26ai Memory\n"
        f"Active (primary). ADB VECTOR IVF + Oracle Text + JSON. Agent: mock_agent.\n"
        f"{plugin._PROMPT_BODY}"
    )

    assert result == expected
