import pytest
import os
import json
from langsync.processor import LocaleProcessor

def test_get_missing_keys():
    source = {
        "key1": "value1",
        "key2": {
            "subkey1": "subvalue1",
            "subkey2": "subvalue2"
        },
        "key3": ""
    }
    target = {
        "key1": "value1",
        "key2": {
            "subkey1": "subvalue1"
        }
    }
    processor = LocaleProcessor(source)
    missing = processor.get_missing_keys(target)
    
    # Missing should be [(['key2', 'subkey2'], 'subvalue2'), (['key3'], "")]
    # Note: key3 is present in source as "", and get_missing_keys check for `not target[key]`
    # Let's verify the logic in _find_keys
    # if rewrite or key not in target or not target[key]:
    
    assert (['key2', 'subkey2'], 'subvalue2') in missing
    assert (['key3'], "") in missing
    assert len(missing) == 2

def test_set_value_by_path():
    data = {}
    LocaleProcessor.set_value_by_path(data, ["a", "b", "c"], "value")
    assert data == {"a": {"b": {"c": "value"}}}
    
    LocaleProcessor.set_value_by_path(data, ["a", "d"], "value2")
    assert data == {"a": {"b": {"c": "value"}, "d": "value2"}}

def test_prune_extra_keys():
    source = {"a": 1, "b": {"c": 2}}
    target = {"a": 1, "b": {"c": 2, "d": 3}, "e": 4}
    
    LocaleProcessor.prune_extra_keys(source, target)
    assert target == {"a": 1, "b": {"c": 2}}

def test_load_save_json(tmp_path):
    file_path = tmp_path / "test.json"
    data = {"hello": "world"}
    
    LocaleProcessor.save_json(str(file_path), data)
    loaded = LocaleProcessor.load_json(str(file_path))
    
    assert data == loaded

def test_load_non_existent():
    assert LocaleProcessor.load_json("non_existent.json") == {}
