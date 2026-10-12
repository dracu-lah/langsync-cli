"""Snapshot tracking for source-key drift detection.

The state file maps every leaf source-key path to a deterministic hash of its
value at the time of the last successful sync. Compared against the live source
on each run, this lets langsync tell apart keys that are genuinely missing in a
target locale from keys whose source value has changed since the last sync.

Schema (version 1, written whenever every locale is caught up):
    {
      "version": 1,
      "hashes": {
        "dotted.path.to.key": "<sha256-hex>",
        ...
      }
    }

Version 2 adds per-locale lag, written only when some locale is behind:
    {
      "version": 2,
      "hashes": {...},
      "behind": {
        "fr": {"dotted.path.to.key": "<hash the fr file was last synced against>"}
      }
    }

"hashes" is the source as of the last run. "behind" records, per locale, the
keys whose source changed while that locale did NOT catch up (it wasn't part
of a `-l` subset run, or its translation failed). Older langsync versions only
read "hashes", so a v2 file stays readable by them.
"""

import hashlib
import json
import os
from collections import OrderedDict

SCHEMA_VERSION = 1
SCHEMA_VERSION_BEHIND = 2
STATE_FILENAME = ".langsync-state.json"


def path_to_key(path):
    """Render a path list into the stable string form used as a snapshot key.

    Dict keys are dotted (`a.b.c`); list indices are bracketed (`a.b[0].c`), so
    every element of an array gets its own snapshot entry and the diff can track
    per-element changes instead of treating the whole array as one opaque leaf.
    """
    parts = []
    for p in path:
        if isinstance(p, bool):
            # bool is an int subclass — guard so a stray bool key isn't bracketed.
            parts.append(("." if parts else "") + str(p))
        elif isinstance(p, int):
            parts.append(f"[{p}]")
        else:
            parts.append(("." if parts else "") + str(p))
    return "".join(parts)


def value_hash(value):
    """Stable SHA-256 over the JSON serialization of any leaf value."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_source_hashes(source_data):
    """Walk every leaf in the source dict and return {path_key: hash}."""
    hashes = {}

    def walk(node, path):
        # Non-empty containers recurse element by element; a list is NOT an
        # opaque leaf. Empty containers (and all scalars) are hashed as leaves so
        # an empty `[]`/`{}` still round-trips instead of vanishing.
        if isinstance(node, dict) and node:
            for k, v in node.items():
                walk(v, path + [k])
        elif isinstance(node, list) and node:
            for i, v in enumerate(node):
                walk(v, path + [i])
        else:
            hashes[path_to_key(path)] = value_hash(node)

    walk(source_data, [])
    return hashes


def default_state_path(messages_dir):
    return os.path.join(messages_dir, STATE_FILENAME)


def _read_state_file(path):
    """Return the parsed state object, or None if missing/corrupt/not a dict."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _string_map(value):
    if not isinstance(value, dict):
        return None
    return {str(k): v for k, v in value.items() if isinstance(v, str)}


def load_state(path):
    """Return (hashes_dict, exists_bool). Missing/invalid files yield ({}, False)."""
    data = _read_state_file(path)
    if data is None:
        return {}, False
    hashes = _string_map(data.get("hashes", {}))
    if hashes is None:
        return {}, False
    return hashes, True


def _write_json(path, payload):
    # Imported lazily: fileio imports this module for path_to_key.
    from .fileio import atomic_write_bytes

    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    atomic_write_bytes(path, text.encode("utf-8"))


def save_state(path, hashes):
    """Write the snapshot deterministically (sorted keys, trailing newline)."""
    payload = OrderedDict()
    payload["version"] = SCHEMA_VERSION
    payload["hashes"] = OrderedDict(sorted(hashes.items()))
    _write_json(path, payload)


class Snapshot:
    """The drift snapshot plus per-locale lag.

    hashes: {key: hash} of the source as of the last run.
    behind: {locale: {key: hash}} — for keys a locale has NOT caught up on,
            the source hash that locale was last synced against.
    exists: whether a state file was found (False means first run/bootstrap).
    """

    def __init__(self, hashes=None, behind=None, exists=False):
        self.hashes = dict(hashes or {})
        self.behind = {loc: dict(m) for loc, m in (behind or {}).items() if m}
        self.exists = exists

    def for_locale(self, locale):
        """The snapshot to classify `locale` against: the global hashes, with
        that locale's lagging keys pinned to their older hashes so the drift
        still shows up as `changed` for it."""
        return {**self.hashes, **self.behind.get(locale, {})}

    def __eq__(self, other):
        return (
            isinstance(other, Snapshot)
            and self.hashes == other.hashes
            and self.behind == other.behind
            and self.exists == other.exists
        )

    def __repr__(self):
        return f"Snapshot(hashes={len(self.hashes)} keys, behind={self.behind!r}, exists={self.exists})"


def load_snapshot(path):
    """Read a v1 or v2 state file. Malformed "behind" entries are ignored
    rather than failing the run: worst case a locale re-syncs a key."""
    data = _read_state_file(path)
    if data is None:
        return Snapshot()
    hashes = _string_map(data.get("hashes", {}))
    if hashes is None:
        return Snapshot()
    behind = {}
    raw_behind = data.get("behind")
    if isinstance(raw_behind, dict):
        for locale, keys in raw_behind.items():
            keys = _string_map(keys)
            if keys:
                behind[str(locale)] = keys
    return Snapshot(hashes, behind, exists=True)


def save_snapshot(path, snapshot):
    """Write a Snapshot. With no lagging locale this is byte-for-byte the v1
    format save_state writes, so existing users see no diff; otherwise v2
    with a sorted "behind" object."""
    behind = {loc: keys for loc, keys in snapshot.behind.items() if keys}
    if not behind:
        save_state(path, snapshot.hashes)
        return
    payload = OrderedDict()
    payload["version"] = SCHEMA_VERSION_BEHIND
    payload["hashes"] = OrderedDict(sorted(snapshot.hashes.items()))
    payload["behind"] = OrderedDict(
        (loc, OrderedDict(sorted(behind[loc].items()))) for loc in sorted(behind)
    )
    _write_json(path, payload)


def advance_snapshot(old, current_hashes, *, processed, all_locales, failed):
    """Compute the snapshot to save after a run.

    The global hashes always move to `current_hashes`. Locales that did not
    fully catch up keep the OLD hash for the keys they missed, in `behind`:

    - Subset run: the source edits key "a", then `langsync -l fr` runs. Without
      lag tracking the global snapshot advances and "de" never sees "a" as
      changed. Here de (not processed) keeps behind {"a": old_hash}; fr (processed)
      has nothing behind. The next `langsync -l de` re-translates "a" for de.
    - Per-locale failure: key "b" changed and failed to translate for "ja" only.
      Only ja keeps "b" behind; fr and de, which succeeded, don't redo it.
    - Catching up: a processed locale with no failures clears its lag.

    Lag is kept only for keys whose old hash differs from the current one and
    that still exist in the source (removed keys drop out). Locales not in
    `all_locales` (deleted locale files) are dropped. Keys are opaque strings,
    so namespace prefixes like "common:" need no special handling.

    processed:   set of locales synced this run (subset of all_locales).
    all_locales: set of every locale in the project.
    failed:      {locale: set of snapshot keys that failed for that locale}.
    """
    failed = failed or {}
    behind = {}
    for locale in all_locales:
        eff = old.for_locale(locale)
        if locale in processed:
            candidates = failed.get(locale, ())
        else:
            candidates = current_hashes
        lag = {
            k: eff[k]
            for k in candidates
            if k in eff and k in current_hashes and eff[k] != current_hashes[k]
        }
        if lag:
            behind[locale] = lag
    return Snapshot(dict(current_hashes), behind, exists=True)
