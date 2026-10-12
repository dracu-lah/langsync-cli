import fnmatch
import re

from . import fileio, icu
from .state import path_to_key, value_hash

REORDER_MODES = ("keep", "source", "alpha")

# Simple placeholders ("{name}", "{{count}}", "%s", "%1$d") carry no words, so
# a string made only of them is legitimately identical in every language.
_PLACEHOLDER_RE = re.compile(r"\{\{?\s*[\w.]+\s*\}?\}|%\d*\$?[sd@]")


def _has_words(value):
    return any(ch.isalpha() for ch in _PLACEHOLDER_RE.sub("", value))


def _is_container(value):
    return isinstance(value, (dict, list))


def _child(container, key):
    """Read container[key] defensively, tolerating type/shape mismatches.

    `key` is a dict key (str) or a list index (int). Returns None when the
    container is the wrong type or the index/key is absent, so callers can treat
    "target has no value here" uniformly.
    """
    if isinstance(key, bool):
        return container.get(key) if isinstance(container, dict) else None
    if isinstance(key, int):
        if isinstance(container, list) and 0 <= key < len(container):
            return container[key]
        return None
    return container.get(key) if isinstance(container, dict) else None


def _assign(container, key, value):
    """Write value into container[key], growing a list with None padding so an
    out-of-order index assignment doesn't raise."""
    if isinstance(key, int) and not isinstance(key, bool):
        while len(container) <= key:
            container.append(None)
        container[key] = value
    else:
        container[key] = value


class LocaleProcessor:
    def __init__(self, source_data):
        self.source_data = source_data

    def classify_keys(self, target_data, snapshot_hashes=None, *, force_rewrite=False,
                      exclude=None, skip_keys=None):
        """Classify every leaf in the source against the target locale and the
        last-known source-state snapshot.

        Returns a dict with these buckets (paths are lists):
            missing_translatable: [(path, value)]  - string source, absent in target
            missing_passthrough:  [(path, value)]  - non-string/empty/excluded source,
                                                     absent in target; copied verbatim
            changed_translatable: [(path, value)]  - string source whose hash differs from snapshot
            changed_passthrough:  [(path, value)]  - non-string/empty/excluded source whose
                                                     hash differs; copied verbatim
            invalid_target:       [(path, value)]  - target string is broken ICU while the
                                                     source parses; needs re-translation
            invalid_source:       [(path, value)]  - source looks like ICU but doesn't parse;
                                                     must never be translated
            unchanged:            [path, ...]
            orphans:              [path, ...]      - present in target but not in source

        Report-only lists (their paths also appear in one of the buckets above):
            identical:      [path, ...]  - target text equals the source text
                                           exactly ("probably untranslated")
            shape_mismatch: [path, ...]  - source and target disagree on the type
                                           at this path (string vs object/array);
                                           the target value gets overwritten

        force_rewrite=True treats every source key as `changed_*`, regardless of
        what the target or snapshot say (except invalid_source, which is always
        excluded from translation, and excluded keys already in the target).

        exclude: fnmatch globs matched (case-sensitively) against the dotted
        snapshot key (`path_to_key`, e.g. "legal.*", "brand.name"). Excluded
        leaves are never sent to the translator: absent or source-changed
        ones are copied verbatim, present ones are left alone — even under
        force_rewrite, since "do not touch" must mean that.

        skip_keys: callable(path) -> bool. A matching node (container or leaf)
        and everything below it is ignored entirely: not classified and not an
        orphan. Used for ARB "@"-metadata.
        """
        result = {
            "missing_translatable": [],
            "missing_passthrough": [],
            "changed_translatable": [],
            "changed_passthrough": [],
            "invalid_target": [],
            "invalid_source": [],
            "unchanged": [],
            "orphans": [],
            "identical": [],
            "shape_mismatch": [],
        }
        ctx = {
            "snapshot": snapshot_hashes or {},
            "result": result,
            "force_rewrite": force_rewrite,
            "exclude": list(exclude or []),
            "skip": skip_keys,
        }
        self._classify(self.source_data, target_data, [], ctx)
        self._collect_orphans(self.source_data, target_data, [], result["orphans"], skip_keys)
        return result

    @staticmethod
    def _is_excluded(path, patterns):
        if not patterns:
            return False
        key = path_to_key(path)
        return any(fnmatch.fnmatchcase(key, pat) for pat in patterns)

    def _classify(self, source, target, path, ctx):
        result = ctx["result"]
        if path and ctx["skip"] is not None and ctx["skip"](path):
            return
        # Recurse into BOTH dicts and (non-empty) lists so array elements are
        # classified — and translated — element by element rather than copied
        # verbatim. Empty containers fall through to the leaf branch so they are
        # copied as-is and re-created in the target.
        if isinstance(source, dict) and source:
            if path and target is not None and not isinstance(target, dict):
                result["shape_mismatch"].append(path)
            for key, value in source.items():
                self._classify(value, _child(target, key), path + [key], ctx)
            return
        if isinstance(source, list) and source:
            if path and target is not None and not isinstance(target, list):
                result["shape_mismatch"].append(path)
            for i, value in enumerate(source):
                self._classify(value, _child(target, i), path + [i], ctx)
            return

        # Leaf: a scalar, an empty string, or an empty container. `target` here
        # is the corresponding target value (or None if absent / shape-mismatched).
        # An empty path means the source root itself is an empty container — there
        # is no key to classify, so emit nothing.
        if not path:
            return
        value = source
        current_path = path
        is_translatable = isinstance(value, str) and value.strip()
        if not _is_container(value) and _is_container(target):
            result["shape_mismatch"].append(current_path)
        target_has_value = (
            target is not None
            and target != ""
            and not _is_container(target)
        )
        prior_hash = ctx["snapshot"].get(path_to_key(current_path))
        hash_changed = prior_hash is not None and prior_hash != value_hash(value)

        # Excluded keys are copied verbatim, never translated, and never
        # overwritten once present (not even by force_rewrite).
        if self._is_excluded(current_path, ctx["exclude"]):
            if not target_has_value:
                result["missing_passthrough"].append((current_path, value))
            elif hash_changed and not ctx["force_rewrite"]:
                result["changed_passthrough"].append((current_path, value))
            else:
                result["unchanged"].append(current_path)
            return

        # A source string that *looks* like ICU (plural/select) but doesn't
        # parse must never be translated: the plain marker path would expose
        # its reserved keywords to the API. Surface it instead.
        if is_translatable and icu.looks_structured(value) and not icu.is_valid(value):
            result["invalid_source"].append((current_path, value))
            return

        if ctx["force_rewrite"]:
            bucket = "changed_translatable" if is_translatable else "changed_passthrough"
            result[bucket].append((current_path, value))
            return

        if not target_has_value:
            bucket = "missing_translatable" if is_translatable else "missing_passthrough"
            result[bucket].append((current_path, value))
            return

        # Present but broken: a target string that no longer parses as ICU
        # while its source does will parse-fail at render and show nothing —
        # worse than a missing key. Flag it for re-translation regardless of
        # what the drift snapshot says.
        if (
            is_translatable
            and "{" in value
            and isinstance(target, str)
            and icu.is_valid(value)
            and not icu.is_valid(target)
        ):
            result["invalid_target"].append((current_path, value))
            return

        if hash_changed:
            bucket = "changed_translatable" if is_translatable else "changed_passthrough"
            result[bucket].append((current_path, value))
        else:
            result["unchanged"].append(current_path)
            # Same text in source and target usually means a key was copied
            # by hand and never translated. Report-only: brand names and
            # words like "OK" are legitimately identical, so we don't act on it.
            if is_translatable and target == value and _has_words(value):
                result["identical"].append(current_path)

    def _collect_orphans(self, source, target, path, out, skip_keys=None):
        # Orphans at any depth: dict keys the source dropped, and list elements
        # past the source's array length. Recurse through both container types.
        # Skipped paths (e.g. ARB metadata) are never orphans.
        if isinstance(target, dict):
            source_keys = source if isinstance(source, dict) else {}
            for key, tval in target.items():
                current_path = path + [key]
                if skip_keys is not None and skip_keys(current_path):
                    continue
                if key not in source_keys:
                    out.append(current_path)
                    continue
                self._collect_orphans(source_keys[key], tval, current_path, out, skip_keys)
        elif isinstance(target, list):
            source_list = source if isinstance(source, list) else []
            for i, tval in enumerate(target):
                current_path = path + [i]
                if skip_keys is not None and skip_keys(current_path):
                    continue
                if i >= len(source_list):
                    out.append(current_path)
                    continue
                self._collect_orphans(source_list[i], tval, current_path, out, skip_keys)

    def get_missing_keys(self, target_data, rewrite=False):
        """Legacy entrypoint. Returns (translatable, passthrough) for keys that
        are missing in the target (plus everything when rewrite=True). Does NOT
        consult a snapshot, so it cannot detect source-value drift — use
        classify_keys for that.
        """
        c = self.classify_keys(target_data, snapshot_hashes=None, force_rewrite=rewrite)
        translatable = c["missing_translatable"] + c["changed_translatable"] + c["invalid_target"]
        passthrough = c["missing_passthrough"] + c["changed_passthrough"]
        return translatable, passthrough

    @staticmethod
    def find_invalid_icu_sources(data):
        """Walk a source tree and return [(path, value)] for every string that
        looks like an ICU plural/select message but does not parse. These keys
        are excluded from translation until the source is fixed."""
        out = []

        def walk(node, path):
            if isinstance(node, dict):
                for key, val in node.items():
                    walk(val, path + [key])
            elif isinstance(node, list):
                for i, val in enumerate(node):
                    walk(val, path + [i])
            elif (
                isinstance(node, str) and node.strip()
                and icu.looks_structured(node) and not icu.is_valid(node)
            ):
                out.append((path, node))

        walk(data, [])
        return out

    @staticmethod
    def set_value_by_path(data, path, value):
        """Set a value in a nested structure given a path list whose elements are
        dict keys (str) or list indices (int).

        Intermediate containers are created to match the next path element (a list
        when the next step is an index, a dict otherwise) and coerced if the
        existing value is the wrong type — this cleanly overwrites a stale value
        left behind when a source key changes container type (array <-> object)."""
        current = data
        for i, key in enumerate(path):
            if i == len(path) - 1:
                _assign(current, key, value)
                return
            next_key = path[i + 1]
            want_list = isinstance(next_key, int) and not isinstance(next_key, bool)
            child = _child(current, key)
            if want_list and not isinstance(child, list):
                child = []
                _assign(current, key, child)
            elif not want_list and not isinstance(child, dict):
                child = {}
                _assign(current, key, child)
            current = child

    @staticmethod
    def remove_by_path(data, path):
        """Delete the leaf at path (dict key or list index). Empty parent
        containers are left in place so nested structure stays diff-stable.

        When pruning several sibling list indices, remove them highest-index
        first so earlier deletions don't shift the ones still to come."""
        if not path:
            return
        current = data
        for key in path[:-1]:
            current = _child(current, key)
            if current is None:
                return
        last = path[-1]
        if isinstance(last, int) and not isinstance(last, bool):
            if isinstance(current, list) and 0 <= last < len(current):
                del current[last]
        elif isinstance(current, dict) and last in current:
            del current[last]

    @staticmethod
    def prune_extra_keys(source, target):
        """Recursively remove keys/elements from target that are absent in source,
        for both dict keys and list indices."""
        if isinstance(source, dict) and isinstance(target, dict):
            for k in [k for k in target if k not in source]:
                del target[k]
            for k, v in source.items():
                if k in target:
                    LocaleProcessor.prune_extra_keys(v, target[k])
        elif isinstance(source, list) and isinstance(target, list):
            # Drop trailing elements the source no longer has, then recurse into
            # the ones that remain.
            del target[len(source):]
            for i, v in enumerate(source):
                LocaleProcessor.prune_extra_keys(v, target[i])

    @staticmethod
    def reorder(source, target, mode):
        """Return a NEW structure with target's dict keys reordered.

        mode "keep":   target as-is (no reordering).
        mode "source": dict keys follow the source's order at every level;
                       target-only keys (orphans) follow, in their existing
                       relative order. Keeps locale files diffable side by
                       side with the source.
        mode "alpha":  dict keys sorted at every level (case-sensitive; stable).

        Lists keep their element order but their elements are reordered too.
        """
        if mode == "keep":
            return target
        if mode not in REORDER_MODES:
            raise ValueError(f"unknown key order {mode!r} (expected one of {', '.join(REORDER_MODES)})")

        def walk(src, node):
            if isinstance(node, dict):
                if mode == "alpha":
                    keys = sorted(node, key=str)
                else:
                    src_keys = src if isinstance(src, dict) else {}
                    keys = [k for k in src_keys if k in node]
                    keys += [k for k in node if k not in src_keys]
                return {k: walk(_child(src, k), node[k]) for k in keys}
            if isinstance(node, list):
                return [walk(_child(src, i), v) for i, v in enumerate(node)]
            return node

        return walk(source, target)

    @staticmethod
    def set_arb_locale(target, locale):
        """Set an ARB file's "@@locale" in place and return the target.

        Flutter expects underscores ("pt_BR", not "pt-BR"). An existing
        "@@locale" keeps its position; a new one goes first, where Flutter's
        own tooling puts it. Non-dict targets are returned unchanged."""
        if not isinstance(target, dict):
            return target
        value = str(locale).replace("-", "_")
        if "@@locale" in target:
            target["@@locale"] = value
            return target
        items = list(target.items())
        target.clear()
        target["@@locale"] = value
        target.update(items)
        return target

    @staticmethod
    def load_json(file_path):
        """Load a locale file (JSON, ARB or YAML by extension). Missing or
        empty files are {}. Use fileio.load to also get its layout."""
        return fileio.load(file_path)[0]

    @staticmethod
    def save_json(file_path, data, style=None):
        """Atomically write a locale file. style=None keeps the layout of the
        file already on disk (default 2-space JSON for a new file)."""
        fileio.save(file_path, data, style)
