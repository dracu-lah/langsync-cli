"""Format-preserving locale file IO.

Locale files live in the user's repo, so every byte we rewrite shows up in
their diffs. `load` remembers how a file was written (indent, newline style,
trailing newline, UTF-8 BOM) and `save` writes it back the same way, so a run
that changes nothing produces no diff and a run that changes one key produces
a one-line diff instead of a whole-file reformat.

Writes are atomic (temp file in the same directory + os.replace): an
interrupted run (Ctrl-C mid-translation, a crash, a full disk) leaves either
the old file or the new one, never a truncated JSON file.

Supported formats: JSON (`.json`), Flutter ARB (`.arb`, which is JSON with
"@"-metadata keys) and YAML (`.yaml`/`.yml`, needs the optional PyYAML).
"""

import json
import os
import tempfile

SUPPORTED_EXTENSIONS = (".json", ".arb", ".yaml", ".yml")
YAML_EXTENSIONS = (".yaml", ".yml")

_BOM = "﻿"
_BOM_BYTES = _BOM.encode("utf-8")


class FormatError(Exception):
    """A locale file could not be read or written in its format (YAML syntax
    error, PyYAML not installed). JSON syntax errors stay json.JSONDecodeError
    because callers already handle that type."""


class FileStyle:
    """How a locale file is laid out on disk, so `save` can reproduce it.

    kind:             "json" or "yaml" (ARB files are kind "json" with arb=True)
    indent:           one indentation unit, e.g. "  ", "    " or "\\t"
    newline:          "\\n" or "\\r\\n"
    trailing_newline: whether the file ends with a newline
    bom:              whether the file started with a UTF-8 byte order mark
    duplicates:       dotted key paths that appeared more than once (JSON only).
                      json.load silently keeps the last value, which usually
                      means a merge conflict was resolved badly — worth a warning.
    """

    def __init__(self, kind="json", indent="  ", newline="\n",
                 trailing_newline=True, bom=False, arb=False, duplicates=None):
        self.kind = kind
        self.indent = indent
        self.newline = newline
        self.trailing_newline = trailing_newline
        self.bom = bom
        self.arb = arb
        self.duplicates = list(duplicates or [])

    @classmethod
    def default_for(cls, path):
        ext = os.path.splitext(str(path))[1].lower()
        if ext in YAML_EXTENSIONS:
            return cls(kind="yaml")
        return cls(kind="json", arb=(ext == ".arb"))

    def __eq__(self, other):
        return isinstance(other, FileStyle) and vars(self) == vars(other)

    def __repr__(self):
        fields = ", ".join(f"{k}={v!r}" for k, v in vars(self).items())
        return f"FileStyle({fields})"


def is_yaml_path(path):
    return os.path.splitext(str(path))[1].lower() in YAML_EXTENSIONS


def locale_from_filename(name):
    """Return the locale code a locale file name stands for, or None.

    Uses os.path.splitext so only the LAST extension is stripped:
    "fr.backup.json" is locale "fr.backup", not a second "fr" file that would
    silently overwrite the real one. Dotfiles (".langsync-state.json") and
    unsupported extensions return None.
    """
    base = os.path.basename(str(name))
    if not base or base.startswith("."):
        return None
    root, ext = os.path.splitext(base)
    if not root or ext.lower() not in SUPPORTED_EXTENSIONS:
        return None
    return root


def _import_yaml():
    try:
        import yaml  # noqa: PLC0415 - optional dependency, imported lazily
    except ImportError:
        raise FormatError(
            "YAML locale files need PyYAML. Install it with: pip install pyyaml"
            " (pipx users: pipx inject langsync pyyaml)"
        ) from None
    return yaml


def _detect_style(text, path):
    """Sniff layout from raw text (BOM already stripped). Never parses."""
    style = FileStyle.default_for(path)
    if not text.strip():
        return style
    style.newline = "\r\n" if "\r\n" in text else "\n"
    style.trailing_newline = text.endswith("\n")
    # The first indented line of a pretty-printed file is one level deep, so
    # its leading whitespace is exactly one indent unit.
    for line in text.splitlines():
        stripped = line.lstrip(" \t")
        if stripped and len(stripped) != len(line):
            style.indent = line[: len(line) - len(stripped)]
            break
    return style


def _parse_json(text):
    """json.loads that keeps the last value of a duplicate key (like json.load)
    and reports where duplicates occurred as dotted paths."""
    seen = []  # (dict object, [duplicate keys]) in construction order

    def hook(pairs):
        obj = {}
        dups = []
        for key, value in pairs:
            if key in obj:
                dups.append(key)
            obj[key] = value
        if dups:
            seen.append((obj, dups))
        return obj

    data = json.loads(text, object_pairs_hook=hook)
    if not seen:
        return data, []

    # The hook runs bottom-up and doesn't know where an object will end up, so
    # locate each offending object in the finished tree by identity.
    from .state import path_to_key

    locations = {}

    def walk(node, path):
        if isinstance(node, dict):
            locations[id(node)] = path
            for k, v in node.items():
                walk(v, path + [k])
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, path + [i])

    walk(data, [])
    duplicates = []
    for obj, dups in seen:
        # An object that was itself a losing duplicate is no longer in the
        # tree; its parent key is already reported, so skip it.
        if id(obj) not in locations:
            continue
        for key in dups:
            dotted = path_to_key(locations[id(obj)] + [key])
            if dotted not in duplicates:
                duplicates.append(dotted)
    return data, duplicates


def _parse_yaml(text):
    yaml = _import_yaml()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        problem = getattr(e, "problem", None) or str(e)
        if mark is not None:
            raise FormatError(
                f"invalid YAML at line {mark.line + 1}, column {mark.column + 1}: {problem}"
            ) from None
        raise FormatError(f"invalid YAML: {problem}") from None
    return ({} if data is None else data), []


def _parse(text, path):
    if text.startswith(_BOM):
        text = text[1:]
    if not text.strip():
        return {}, []
    if is_yaml_path(path):
        return _parse_yaml(text)
    return _parse_json(text)


def parse_text(text, path):
    """Parse locale file content, choosing the format from `path`'s extension.

    Empty/whitespace-only text is {}. Raises json.JSONDecodeError for bad JSON
    and FormatError for bad YAML or a missing PyYAML."""
    return _parse(text, path)[0]


def load(path):
    """Return (data, FileStyle) for a locale file.

    A missing or empty file (`touch fr.json`) is ({}, default style)."""
    path = str(path)
    if not os.path.exists(path):
        return {}, FileStyle.default_for(path)
    # newline="" keeps "\r\n" intact so the newline style can be detected.
    with open(path, "r", encoding="utf-8", newline="") as f:
        text = f.read()
    bom = text.startswith(_BOM)
    if bom:
        text = text[1:]
    style = _detect_style(text, path)
    style.bom = bom
    data, style.duplicates = _parse(text, path)
    return data, style


def sniff_style(path):
    """Layout of an existing file without parsing it (default when missing or
    unreadable)."""
    path = str(path)
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return FileStyle.default_for(path)
    bom = text.startswith(_BOM)
    style = _detect_style(text[1:] if bom else text, path)
    style.bom = bom
    return style


def dumps(data, style):
    """Serialise `data` exactly as `save` would write it (as text, BOM included)."""
    if style.kind == "yaml":
        yaml = _import_yaml()
        text = yaml.safe_dump(
            data, allow_unicode=True, sort_keys=False,
            default_flow_style=False, width=float("inf"),
        )
        text = text.rstrip("\n")
    else:
        text = json.dumps(data, indent=style.indent, ensure_ascii=False)
    if style.trailing_newline:
        text += "\n"
    if style.newline != "\n":
        text = text.replace("\n", style.newline)
    if style.bom:
        text = _BOM + text
    return text


def _default_mode():
    # NamedTemporaryFile creates 0600; a brand-new locale file should get the
    # same permissions open() would have given it.
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def atomic_write_bytes(path, payload):
    """Write bytes to `path` via a temp file + os.replace, creating parent
    directories and keeping the existing file's permission bits."""
    path = str(path)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    try:
        mode = os.stat(path).st_mode & 0o7777
    except OSError:
        mode = _default_mode()
    tmp = tempfile.NamedTemporaryFile(
        delete=False, dir=directory,
        prefix="." + os.path.basename(path) + ".", suffix=".tmp",
    )
    try:
        with tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp.name, mode)
        os.replace(tmp.name, path)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def save(path, data, style=None):
    """Atomically write `data` to `path` in `style`.

    style=None reuses the layout of the file already on disk (so callers that
    never loaded a style still don't reformat the user's file), or the
    default for the extension when the file is new."""
    path = str(path)
    if style is None:
        style = sniff_style(path) if os.path.exists(path) else FileStyle.default_for(path)
    atomic_write_bytes(path, dumps(data, style).encode("utf-8"))

