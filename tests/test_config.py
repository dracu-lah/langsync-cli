import os
import json
import pytest
from langsync.config import load_config, get_default_config, save_config

def test_get_default_config():
    config = get_default_config()
    assert config['source'] == 'messages/en-GB.json'
    assert 'whitelist' in config
    assert isinstance(config['whitelist'], list)

def test_save_and_load_config(tmp_path):
    config_file = tmp_path / "langsync.json"
    custom_config = {
        "source": "src/locales/en.json",
        "dir": "src/locales",
        "max_parallel_locales": 10,
        "delay_between_requests": 0.5,
        "whitelist": ["MyCompany"]
    }
    save_config(str(config_file), custom_config)
    
    # Need to change CWD or pass path
    loaded, loaded_path = load_config(str(config_file))
    
    assert loaded_path == str(config_file)
    assert loaded['source'] == "src/locales/en.json"
    assert loaded['max_parallel_locales'] == 10
    assert loaded['delay_between_requests'] == 0.5
    # Whitelist is merged with default
    assert "MyCompany" in loaded['whitelist']
    assert "Lascade" in loaded['whitelist']

def test_load_config_invalid_json(tmp_path):
    config_file = tmp_path / "langsync.json"
    with open(config_file, 'w') as f:
        f.write("invalid json")
    
    loaded, loaded_path = load_config(str(config_file))
    # Should return default config if invalid
    assert loaded == get_default_config()
