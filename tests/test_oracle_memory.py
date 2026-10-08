import sys
import os
import importlib.util

# Ensure mock_env is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), 'mock_env')))

spec = importlib.util.spec_from_file_location("oracle_plugin", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "__init__.py")))
oracle_plugin = importlib.util.module_from_spec(spec)
sys.modules["oracle_plugin"] = oracle_plugin
spec.loader.exec_module(oracle_plugin)

def test_contains_query_empty():
    provider = oracle_plugin.OracleMemoryProvider()
    assert provider._contains_query(None) == "memory"
    assert provider._contains_query("") == "memory"
    assert provider._contains_query("   ") == "memory"

def test_contains_query_short_words():
    provider = oracle_plugin.OracleMemoryProvider()
    assert provider._contains_query("a an it is to do") == "memory"

def test_contains_query_basic():
    provider = oracle_plugin.OracleMemoryProvider()
    assert provider._contains_query("hello world") == "hello ACCUM world"

def test_contains_query_punctuation():
    provider = oracle_plugin.OracleMemoryProvider()
    assert provider._contains_query("hello, world! how's it going?") == "hello ACCUM world ACCUM how ACCUM going"

def test_contains_query_max_tokens():
    provider = oracle_plugin.OracleMemoryProvider()
    long_query = "one two three four five six seven eight nine ten"
    expected = "one ACCUM two ACCUM three ACCUM four ACCUM five ACCUM six ACCUM seven ACCUM eight"
    assert provider._contains_query(long_query) == expected

def test_contains_query_mixed():
    provider = oracle_plugin.OracleMemoryProvider()
    assert provider._contains_query("  foo! bar,  xyz: a bb ccc ") == "foo ACCUM bar ACCUM xyz ACCUM ccc"

def test_contains_query_hyphen():
    provider = oracle_plugin.OracleMemoryProvider()
    assert provider._contains_query("top-secret info") == "top-secret ACCUM info"
