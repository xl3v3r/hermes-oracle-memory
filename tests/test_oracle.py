import sys
import importlib.util
import os

# Get path to root __init__.py
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
init_path = os.path.join(root_dir, "__init__.py")

spec = importlib.util.spec_from_file_location("oracle_plugin", init_path)
oracle_plugin = importlib.util.module_from_spec(spec)
sys.modules["oracle_plugin"] = oracle_plugin
spec.loader.exec_module(oracle_plugin)

def test_recall_status_none():
    provider = oracle_plugin.OracleMemoryProvider()
    provider._prefetch_count = 0
    assert provider.recall_status() is None

def test_recall_status_positive():
    provider = oracle_plugin.OracleMemoryProvider()
    provider._prefetch_count = 5
    status = provider.recall_status()
    assert status is not None
    assert status.provider_label == "Oracle 26ai"
    assert status.count == 5

def test_recall_status_negative():
    provider = oracle_plugin.OracleMemoryProvider()
    provider._prefetch_count = -1
    assert provider.recall_status() is None
