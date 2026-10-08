import pytest
from unittest.mock import patch, MagicMock

import __init__ as plugin_init

def test_is_available_no_oracledb():
    provider = plugin_init.OracleMemoryProvider()
    with patch.dict('sys.modules', {'oracledb': None}):
        assert provider.is_available() == False

def test_is_available_missing_secrets():
    provider = plugin_init.OracleMemoryProvider()
    with patch.dict('sys.modules', {'oracledb': MagicMock()}):
        with patch.object(plugin_init, 'get_secret', side_effect=lambda k, *args: None):
            assert provider.is_available() == False

def test_is_available_some_missing_secrets():
    provider = plugin_init.OracleMemoryProvider()
    secrets = {
        "OCI_DB_USER": "user",
        "OCI_DB_PASSWORD": None,
        "OCI_DB_DSN": "dsn"
    }
    with patch.dict('sys.modules', {'oracledb': MagicMock()}):
        with patch.object(plugin_init, 'get_secret', side_effect=lambda k, *args: secrets.get(k)):
            assert provider.is_available() == False

def test_is_available_all_present():
    provider = plugin_init.OracleMemoryProvider()
    secrets = {
        "OCI_DB_USER": "user",
        "OCI_DB_PASSWORD": "password",
        "OCI_DB_DSN": "dsn"
    }
    with patch.dict('sys.modules', {'oracledb': MagicMock()}):
        with patch.object(plugin_init, 'get_secret', side_effect=lambda k, *args: secrets.get(k)):
            assert provider.is_available() == True
