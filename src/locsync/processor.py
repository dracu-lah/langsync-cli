import json
import os

class LocaleProcessor:
    def __init__(self, source_data):
        self.source_data = source_data

    def get_missing_keys(self, target_data):
        """Returns a list of tuples (path_list, value) for missing/empty keys."""
        missing = []
        self._find_missing(self.source_data, target_data, [], missing)
        return missing

    def _find_missing(self, source, target, path, missing):
        if not isinstance(source, dict):
            return

        for key, value in source.items():
            current_path = path + [key]
            if isinstance(value, dict):
                if key not in target or not isinstance(target[key], dict):
                    target[key] = {}
                self._find_missing(value, target[key], current_path, missing)
            else:
                if key not in target or not target[key]:
                    missing.append((current_path, value))

    @staticmethod
    def set_value_by_path(data, path, value):
        """Sets a value in a nested dictionary given a path list."""
        current = data
        for i, key in enumerate(path):
            if i == len(path) - 1:
                current[key] = value
            else:
                if key not in current:
                    current[key] = {}
                current = current[key]

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
