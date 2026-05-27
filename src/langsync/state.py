"""Snapshot tracking for source-key drift detection.

The state file maps every leaf source-key path to a deterministic hash of its
value at the time of the last successful sync. Compared against the live source
on each run, this lets langsync tell apart keys that are genuinely missing in a
target locale from keys whose source value has changed since the last sync.

Schema:
    {
      "version": 1,
      "hashes": {
        "dotted.path.to.key": "<sha256-hex>",
        ...
      }
    }
"""

import hashlib
import json
import os
from collections import OrderedDict

SCHEMA_VERSION = 1
STATE_FILENAME = ".langsync-state.json"


def path_to_key(path):
    """Join a path list into the dotted form used as a snapshot key."""
    return ".".join(str(p) for p in path)


def value_hash(value):
    """Stable SHA-256 over the JSON serialization of any leaf value."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_source_hashes(source_data):
    """Walk every leaf in the source dict and return {path_key: hash}."""
    hashes = {}

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + [k])
        else:
            hashes[path_to_key(path)] = value_hash(node)

    walk(source_data, [])
    return hashes


def default_state_path(messages_dir):
    return os.path.join(messages_dir, STATE_FILENAME)


def load_state(path):
    """Return (hashes_dict, exists_bool). Missing/invalid files yield ({}, False)."""
    if not path or not os.path.exists(path):
        return {}, False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}, False
    if not isinstance(data, dict):
        return {}, False
    hashes = data.get("hashes", {})
    if not isinstance(hashes, dict):
        return {}, False
    return {str(k): str(v) for k, v in hashes.items() if isinstance(v, str)}, True


def save_state(path, hashes):
    """Write the snapshot deterministically (sorted keys, trailing newline)."""
    payload = OrderedDict()
    payload["version"] = SCHEMA_VERSION
    payload["hashes"] = OrderedDict(sorted(hashes.items()))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
