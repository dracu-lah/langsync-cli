import json
import os

from .state import path_to_key, value_hash


class LocaleProcessor:
    def __init__(self, source_data):
        self.source_data = source_data

    def classify_keys(self, target_data, snapshot_hashes=None, *, force_rewrite=False):
        """Classify every leaf in the source against the target locale and the
        last-known source-state snapshot.

        Returns a dict with these buckets (paths are lists):
            missing_translatable: [(path, value)]  - string source, absent in target
            missing_passthrough:  [(path, value)]  - non-string/empty source, absent in target
            changed_translatable: [(path, value)]  - string source whose hash differs from snapshot
            changed_passthrough:  [(path, value)]  - non-string/empty source whose hash differs
            unchanged:            [path, ...]
            orphans:              [path, ...]      - present in target but not in source

        force_rewrite=True treats every source key as `changed_*`, regardless of
        what the target or snapshot say.
        """
        result = {
            "missing_translatable": [],
            "missing_passthrough": [],
            "changed_translatable": [],
            "changed_passthrough": [],
            "unchanged": [],
            "orphans": [],
        }
        snapshot_hashes = snapshot_hashes or {}
        self._classify(self.source_data, target_data, [], snapshot_hashes, result, force_rewrite)
        self._collect_orphans(self.source_data, target_data, [], result["orphans"])
        return result

    def _classify(self, source, target, path, snapshot_hashes, result, force_rewrite):
        if not isinstance(source, dict):
            return
        for key, value in source.items():
            current_path = path + [key]
            if isinstance(value, dict):
                if key not in target or not isinstance(target[key], dict):
                    target[key] = {}
                self._classify(value, target[key], current_path, snapshot_hashes, result, force_rewrite)
                continue

            is_translatable = isinstance(value, str) and value.strip()
            target_has_value = (
                isinstance(target, dict)
                and key in target
                and target[key] not in (None, "")
                and not isinstance(target[key], dict)
            )

            if force_rewrite:
                bucket = "changed_translatable" if is_translatable else "changed_passthrough"
                result[bucket].append((current_path, value))
                continue

            if not target_has_value:
                bucket = "missing_translatable" if is_translatable else "missing_passthrough"
                result[bucket].append((current_path, value))
                continue

            prior_hash = snapshot_hashes.get(path_to_key(current_path))
            if prior_hash is not None and prior_hash != value_hash(value):
                bucket = "changed_translatable" if is_translatable else "changed_passthrough"
                result[bucket].append((current_path, value))
            else:
                result["unchanged"].append(current_path)

    def _collect_orphans(self, source, target, path, out):
        if not isinstance(target, dict):
            return
        source_keys = source if isinstance(source, dict) else {}
        for key, tval in target.items():
            current_path = path + [key]
            if key not in source_keys:
                out.append(current_path)
                continue
            if isinstance(tval, dict):
                child_source = source_keys[key] if isinstance(source_keys.get(key), dict) else {}
                self._collect_orphans(child_source, tval, current_path, out)

    def get_missing_keys(self, target_data, rewrite=False):
        """Legacy entrypoint. Returns (translatable, passthrough) for keys that
        are missing in the target (plus everything when rewrite=True). Does NOT
        consult a snapshot, so it cannot detect source-value drift — use
        classify_keys for that.
        """
        c = self.classify_keys(target_data, snapshot_hashes=None, force_rewrite=rewrite)
        translatable = c["missing_translatable"] + c["changed_translatable"]
        passthrough = c["missing_passthrough"] + c["changed_passthrough"]
        return translatable, passthrough

    @staticmethod
    def set_value_by_path(data, path, value):
        """Sets a value in a nested dictionary given a path list."""
        current = data
        for i, key in enumerate(path):
            if i == len(path) - 1:
                current[key] = value
            else:
                if key not in current or not isinstance(current[key], dict):
                    current[key] = {}
                current = current[key]

    @staticmethod
    def remove_by_path(data, path):
        """Delete the leaf at path. Empty parent dicts are left in place so
        nested structure stays diff-stable."""
        if not path:
            return
        current = data
        for key in path[:-1]:
            if not isinstance(current, dict) or key not in current:
                return
            current = current[key]
        if isinstance(current, dict) and path[-1] in current:
            del current[path[-1]]

    @staticmethod
    def prune_extra_keys(source, target):
        """Removes keys from target that are not in source."""
        if not isinstance(source, dict) or not isinstance(target, dict):
            return

        keys_to_remove = [k for k in target if k not in source]
        for k in keys_to_remove:
            del target[k]

        for k, v in source.items():
            if k in target and isinstance(v, dict):
                LocaleProcessor.prune_extra_keys(v, target[k])

    @staticmethod
    def load_json(file_path):
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}

    @staticmethod
    def save_json(file_path, data):
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
