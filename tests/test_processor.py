import pytest
import os
import json
from langsync.processor import LocaleProcessor
from langsync.state import compute_source_hashes, value_hash

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
    translatable, passthrough = processor.get_missing_keys(target)

    # Non-empty missing values go to `translatable` and are sent to the translator.
    assert translatable == [(['key2', 'subkey2'], 'subvalue2')]

    # Empty source values are copied as-is rather than sent to the translator,
    # so the same key isn't re-translated to empty on every run.
    assert passthrough == [(['key3'], "")]


def test_get_missing_keys_skips_filled_targets():
    source = {"a": "hello", "b": "world"}
    target = {"a": "hola"}
    processor = LocaleProcessor(source)
    translatable, passthrough = processor.get_missing_keys(target)

    assert translatable == [(["b"], "world")]
    assert passthrough == []


def test_get_missing_keys_rewrite_includes_all():
    source = {"a": "hello", "b": ""}
    target = {"a": "hola", "b": ""}
    processor = LocaleProcessor(source)
    translatable, passthrough = processor.get_missing_keys(target, rewrite=True)

    assert translatable == [(["a"], "hello")]
    assert passthrough == [(["b"], "")]

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


def test_classify_missing_only():
    source = {"a": "Hello", "b": "World"}
    target = {"a": "Hola"}
    c = LocaleProcessor(source).classify_keys(target, snapshot_hashes={})

    assert c["missing_translatable"] == [(["b"], "World")]
    assert c["changed_translatable"] == []
    assert c["unchanged"] == [["a"]]
    assert c["orphans"] == []


def test_classify_detects_source_drift():
    # Snapshot recorded an OLD source value for `a`. Live source now differs.
    source = {"a": "Hi", "b": "World"}
    target = {"a": "Hola", "b": "Mundo"}
    snapshot = {"a": value_hash("Hello"), "b": value_hash("World")}

    c = LocaleProcessor(source).classify_keys(target, snapshot_hashes=snapshot)

    assert c["changed_translatable"] == [(["a"], "Hi")]
    assert c["missing_translatable"] == []
    assert c["unchanged"] == [["b"]]


def test_classify_first_run_bootstrap_treats_filled_targets_as_unchanged():
    # No snapshot yet (empty hashes). Filled target keys must be unchanged.
    source = {"a": "Hello", "b": "World"}
    target = {"a": "Hola", "b": "Mundo"}
    c = LocaleProcessor(source).classify_keys(target, snapshot_hashes={})

    assert c["changed_translatable"] == []
    assert c["missing_translatable"] == []
    assert set(map(tuple, c["unchanged"])) == {("a",), ("b",)}


def test_classify_orphans_collected():
    source = {"a": "Hello"}
    target = {"a": "Hola", "b": "Mundo", "nested": {"c": "Hi"}}
    c = LocaleProcessor(source).classify_keys(target, snapshot_hashes={})

    orphan_paths = {tuple(p) for p in c["orphans"]}
    assert orphan_paths == {("b",), ("nested",)}


def test_classify_force_rewrite_marks_all_as_changed():
    source = {"a": "Hello", "b": "World"}
    target = {"a": "Hola", "b": "Mundo"}
    snapshot = compute_source_hashes(source)  # everything matches

    c = LocaleProcessor(source).classify_keys(target, snapshot_hashes=snapshot, force_rewrite=True)

    assert {tuple(p) for p, _ in c["changed_translatable"]} == {("a",), ("b",)}
    assert c["unchanged"] == []


def test_remove_by_path():
    data = {"a": 1, "b": {"c": 2, "d": 3}}
    LocaleProcessor.remove_by_path(data, ["b", "c"])
    assert data == {"a": 1, "b": {"d": 3}}

    LocaleProcessor.remove_by_path(data, ["nope"])
    assert data == {"a": 1, "b": {"d": 3}}


def test_remove_by_path_keeps_parent_dict():
    # Removing the only child should NOT delete the parent — that would
    # introduce structural diffs the user didn't ask for.
    data = {"a": {"only": "x"}}
    LocaleProcessor.remove_by_path(data, ["a", "only"])
    assert data == {"a": {}}
