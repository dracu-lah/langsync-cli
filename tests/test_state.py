import json
import os

from langsync.state import (
    SCHEMA_VERSION,
    STATE_FILENAME,
    compute_source_hashes,
    default_state_path,
    load_state,
    path_to_key,
    save_state,
    value_hash,
)


def test_value_hash_is_stable_and_distinct():
    assert value_hash("hello") == value_hash("hello")
    assert value_hash("hello") != value_hash("hello!")
    # Numbers and strings hash differently — the serialization differs.
    assert value_hash(1) != value_hash("1")


def test_compute_source_hashes_walks_nested_leaves():
    source = {"a": "Hello", "b": {"c": "World", "d": ""}}
    hashes = compute_source_hashes(source)

    assert set(hashes.keys()) == {"a", "b.c", "b.d"}
    assert hashes["a"] == value_hash("Hello")
    assert hashes["b.c"] == value_hash("World")
    assert hashes["b.d"] == value_hash("")


def test_save_state_is_deterministic(tmp_path):
    path = tmp_path / "state.json"
    hashes_unsorted = {"b": "x" * 64, "a": "y" * 64}
    save_state(str(path), hashes_unsorted)

    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    parsed = json.loads(raw)
    assert parsed["version"] == SCHEMA_VERSION
    # Keys should be sorted in the on-disk order.
    keys_in_order = list(parsed["hashes"].keys())
    assert keys_in_order == sorted(keys_in_order)


def test_load_state_missing_returns_empty_marker(tmp_path):
    hashes, exists = load_state(str(tmp_path / "missing.json"))
    assert exists is False
    assert hashes == {}


def test_load_state_corrupt_returns_empty(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    hashes, exists = load_state(str(path))
    assert exists is False
    assert hashes == {}


def test_round_trip(tmp_path):
    path = tmp_path / "state.json"
    hashes = {"a": value_hash("x"), "b.c": value_hash("y")}
    save_state(str(path), hashes)
    loaded, exists = load_state(str(path))
    assert exists is True
    assert loaded == hashes


def test_default_state_path_is_inside_dir():
    assert default_state_path("messages").endswith(os.path.join("messages", STATE_FILENAME))


def test_path_to_key_joins_with_dot():
    assert path_to_key(["a", "b", "c"]) == "a.b.c"


def test_path_to_key_brackets_list_indices():
    assert path_to_key(["items", 0, "title"]) == "items[0].title"
    assert path_to_key(["a", 1, 2]) == "a[1][2]"


def test_compute_source_hashes_walks_into_arrays():
    # Array elements — strings and object leaves inside arrays — get their own
    # snapshot entries instead of the whole array hashing as one opaque leaf.
    source = {
        "arr": [{"label": "Window seat"}, "plain"],
        "faq": {"items": [{"question": "Q1"}]},
    }
    hashes = compute_source_hashes(source)

    assert set(hashes.keys()) == {"arr[0].label", "arr[1]", "faq.items[0].question"}
    assert hashes["arr[0].label"] == value_hash("Window seat")
    assert hashes["arr[1]"] == value_hash("plain")


def test_compute_source_hashes_keeps_empty_array_as_leaf():
    hashes = compute_source_hashes({"empty": [], "filled": ["x"]})
    assert set(hashes.keys()) == {"empty", "filled[0]"}
