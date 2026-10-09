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
tools_registry_mod.tool_error = MagicMock(return_value="error")
tools_mod.registry = tools_registry_mod

sys.modules['tools'] = tools_mod
sys.modules['tools.registry'] = tools_registry_mod
