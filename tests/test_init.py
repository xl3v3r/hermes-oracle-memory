import pytest
import sys
import os

sys.path.insert(0, os.path.abspath('.'))

import importlib.util
spec = importlib.util.spec_from_file_location("plugin_init", "./__init__.py")
plugin_init = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin_init)

OracleMemoryProvider = plugin_init.OracleMemoryProvider

def test_get_config_schema():
    provider = OracleMemoryProvider()
    schema = provider.get_config_schema()

    assert isinstance(schema, list)
    assert len(schema) == 5

    # Check that each item in the schema has expected keys
    expected_keys = {"key", "description", "secret", "required"}
    for item in schema:
        assert isinstance(item, dict)
        assert expected_keys.issubset(item.keys())
        assert isinstance(item["key"], str)
        assert isinstance(item["description"], str)
        assert isinstance(item["secret"], bool)
        assert isinstance(item["required"], bool)

        # Check specific items that we know should be there
        if item["key"] == "user":
            assert item["env_var"] == "OCI_DB_USER"
            assert item["required"] is True
            assert item["secret"] is True
        elif item["key"] == "password":
            assert item["env_var"] == "OCI_DB_PASSWORD"
            assert item["required"] is True
            assert item["secret"] is True
        elif item["key"] == "dsn":
            assert item["env_var"] == "OCI_DB_DSN"
            assert item["required"] is True
            assert item["secret"] is True
        elif item["key"] == "table_name":
            assert item["env_var"] == "HERMES_ORACLE_TABLE"
            assert item["default"] == "hermes_agent_memory"
            assert item["required"] is False
            assert item["secret"] is False
        elif item["key"] == "onnx_model_name":
            assert item["env_var"] == "HERMES_ORACLE_ONNX_MODEL"
            assert item["required"] is False
            assert item["secret"] is False

    # Verify we got all the expected top-level keys
    keys = {item["key"] for item in schema}
    assert {"user", "password", "dsn", "table_name", "onnx_model_name"} == keys
