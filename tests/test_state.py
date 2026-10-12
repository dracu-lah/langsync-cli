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


# --- Per-locale snapshot lag -------------------------------------------------------

from langsync.state import Snapshot, advance_snapshot, load_snapshot, save_snapshot  # noqa: E402

H_OLD = value_hash("old")
H_NEW = value_hash("new")
H_X = value_hash("x")


def test_load_snapshot_reads_v1(tmp_path):
    path = tmp_path / "state.json"
    save_state(str(path), {"a": H_X})
    snap = load_snapshot(str(path))
    assert snap.exists is True
    assert snap.hashes == {"a": H_X}
    assert snap.behind == {}
    assert snap.for_locale("fr") == {"a": H_X}


def test_load_snapshot_missing(tmp_path):
    snap = load_snapshot(str(tmp_path / "nope.json"))
    assert snap.exists is False and snap.hashes == {} and snap.behind == {}


def test_save_snapshot_without_lag_is_identical_to_v1(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    save_state(str(a), {"x": H_X, "a": H_OLD})
    save_snapshot(str(b), Snapshot({"x": H_X, "a": H_OLD}, {"fr": {}}))
    assert a.read_bytes() == b.read_bytes()
    assert json.loads(b.read_text())["version"] == 1


def test_snapshot_v2_round_trip_and_old_reader(tmp_path):
    path = tmp_path / "state.json"
    snap = Snapshot({"b": H_NEW, "a": H_X}, {"fr": {"b": H_OLD}, "de": {"b": H_OLD}}, exists=True)
    save_snapshot(str(path), snap)
    raw = json.loads(path.read_text())
    assert raw["version"] == 2
    assert list(raw["behind"]) == ["de", "fr"]
    assert list(raw["hashes"]) == ["a", "b"]
    assert load_snapshot(str(path)) == snap
    # Older langsync versions only read "hashes".
    assert load_state(str(path)) == ({"b": H_NEW, "a": H_X}, True)
    assert snap.for_locale("fr") == {"a": H_X, "b": H_OLD}
    assert snap.for_locale("ja") == {"a": H_X, "b": H_NEW}


def test_load_snapshot_ignores_malformed_behind(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "version": 2,
        "hashes": {"a": H_X},
        "behind": {"fr": "nope", "de": {"a": 5, "b": H_OLD}, "ja": {}},
    }))
    snap = load_snapshot(str(path))
    assert snap.hashes == {"a": H_X}
    assert snap.behind == {"de": {"b": H_OLD}}

    path.write_text(json.dumps({"version": 2, "hashes": {"a": H_X}, "behind": []}))
    assert load_snapshot(str(path)).behind == {}


def test_advance_subset_run_keeps_other_locales_behind():
    old = Snapshot({"a": H_OLD, "b": H_X}, exists=True)
    current = {"a": H_NEW, "b": H_X}
    new = advance_snapshot(old, current, processed={"fr"}, all_locales={"fr", "de"}, failed={})
    assert new.hashes == current
    assert new.behind == {"de": {"a": H_OLD}}
    # de still sees "a" as changed; fr does not.
    assert new.for_locale("de")["a"] == H_OLD
    assert new.for_locale("fr")["a"] == H_NEW


def test_advance_failure_lags_only_that_locale():
    old = Snapshot({"a": H_OLD, "b": H_OLD}, exists=True)
    current = {"a": H_NEW, "b": H_NEW}
    new = advance_snapshot(
        old, current, processed={"fr", "de", "ja"}, all_locales={"fr", "de", "ja"},
        failed={"ja": {"b"}},
    )
    assert new.behind == {"ja": {"b": H_OLD}}


def test_advance_failure_on_new_key_records_nothing():
    # A brand-new key that failed is still missing in the target, so the next
    # run retries it without any lag entry.
    old = Snapshot({"a": H_X}, exists=True)
    new = advance_snapshot(old, {"a": H_X, "n": H_NEW}, processed={"fr"}, all_locales={"fr"}, failed={"fr": {"n"}})
    assert new.behind == {}


def test_advance_catching_up_clears_behind():
    old = Snapshot({"a": H_NEW}, {"de": {"a": H_OLD}}, exists=True)
    new = advance_snapshot(old, {"a": H_NEW}, processed={"de"}, all_locales={"fr", "de"}, failed={})
    assert new.behind == {}


def test_advance_unprocessed_locale_keeps_older_lag_across_runs():
    # de missed edit 1 (old -> mid) and edit 2 (mid -> new); it must stay pinned
    # to the hash it was really synced against.
    h_mid = value_hash("mid")
    old = Snapshot({"a": h_mid}, {"de": {"a": H_OLD}}, exists=True)
    new = advance_snapshot(old, {"a": H_NEW}, processed={"fr"}, all_locales={"fr", "de"}, failed={})
    assert new.behind == {"de": {"a": H_OLD}}


def test_advance_lag_resolved_when_source_reverts():
    old = Snapshot({"a": H_NEW}, {"de": {"a": H_OLD}}, exists=True)
    new = advance_snapshot(old, {"a": H_OLD}, processed={"fr"}, all_locales={"fr", "de"}, failed={})
    assert new.behind == {}


def test_advance_drops_removed_keys_and_locales():
    old = Snapshot({"a": H_NEW, "gone": H_NEW}, {"de": {"gone": H_OLD}, "it": {"a": H_OLD}}, exists=True)
    new = advance_snapshot(old, {"a": H_NEW}, processed={"fr"}, all_locales={"fr", "de"}, failed={})
    assert new.behind == {}
    assert new.hashes == {"a": H_NEW}


def test_advance_namespaced_keys_are_opaque():
    old = Snapshot({"common:hi": H_OLD}, exists=True)
    new = advance_snapshot(old, {"common:hi": H_NEW}, processed=set(), all_locales={"fr"}, failed={})
    assert new.behind == {"fr": {"common:hi": H_OLD}}


def test_save_state_is_atomic(tmp_path):
    path = tmp_path / "state.json"
    save_state(str(path), {"a": H_X})
    save_snapshot(str(path), Snapshot({"a": H_X}, {"fr": {"a": H_OLD}}))
    assert os.listdir(str(tmp_path)) == ["state.json"]
