import sys
import os
from unittest.mock import MagicMock

# Mock out modules that are not available in this test environment
sys.modules['agent'] = MagicMock()
sys.modules['agent.memory_provider'] = MagicMock()
sys.modules['agent.secret_scope'] = MagicMock()
sys.modules['tools.registry'] = MagicMock()

# Set up fake classes that __init__.py expects to inherit from or use
class MemoryProvider:
    pass

sys.modules['agent.memory_provider'].MemoryProvider = MemoryProvider
sys.modules['agent.memory_provider'].RecallStatus = MagicMock()
sys.modules['agent.memory_provider'].is_trivial_prompt = MagicMock()

sys.modules['agent.secret_scope'].get_secret = MagicMock(return_value="mocked")
sys.modules['tools.registry'].tool_error = MagicMock(return_value="mocked_error")
