import builtins
import json
import os
import stat
import sys

import pytest

from langsync import fileio
from langsync.fileio import (
    SUPPORTED_EXTENSIONS,
    FileStyle,
    FormatError,
    load,
    locale_from_filename,
    parse_text,
    save,
)


def _round_trip(path, raw_bytes):
    path.write_bytes(raw_bytes)
    data, style = load(str(path))
    save(str(path), data, style)
    return path.read_bytes(), data, style


# --- Style preservation ------------------------------------------------------

NESTED = {"a": "Hello", "b": {"c": "World", "d": ["x", "y"]}, "e": {}}


@pytest.mark.parametrize("indent", ["  ", "    ", "\t"])
def test_indent_round_trip_is_byte_identical(tmp_path, indent):
    raw = (json.dumps(NESTED, indent=indent, ensure_ascii=False) + "\n").encode("utf-8")
    out, data, style = _round_trip(tmp_path / "fr.json", raw)
    assert out == raw
    assert data == NESTED
    assert style.indent == indent


def test_crlf_round_trip(tmp_path):
    raw = (json.dumps(NESTED, indent=2) + "\n").replace("\n", "\r\n").encode("utf-8")
    out, _, style = _round_trip(tmp_path / "fr.json", raw)
    assert style.newline == "\r\n"
    assert out == raw


def test_no_trailing_newline_round_trip(tmp_path):
    raw = json.dumps(NESTED, indent=2).encode("utf-8")
    out, _, style = _round_trip(tmp_path / "fr.json", raw)
    assert style.trailing_newline is False
    assert out == raw


def test_bom_round_trip(tmp_path):
    raw = b"\xef\xbb\xbf" + (json.dumps(NESTED, indent=4) + "\n").encode("utf-8")
    out, data, style = _round_trip(tmp_path / "fr.json", raw)
    assert style.bom is True
    assert data == NESTED
    assert out == raw


def test_non_ascii_is_written_verbatim(tmp_path):
    raw = (json.dumps({"k": "Größe 日本"}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    out, _, _ = _round_trip(tmp_path / "de.json", raw)
    assert out == raw


def test_save_without_style_keeps_existing_layout(tmp_path):
    path = tmp_path / "fr.json"
    path.write_bytes(b'{\r\n\t"a": "x"\r\n}')
    save(str(path), {"a": "y"})
    assert path.read_bytes() == b'{\r\n\t"a": "y"\r\n}'


def test_save_new_file_uses_default_style(tmp_path):
    path = tmp_path / "nested" / "dir" / "fr.json"
    save(str(path), {"a": "x"})
    assert path.read_text(encoding="utf-8") == '{\n  "a": "x"\n}\n'


# --- Empty / missing / invalid ----------------------------------------------


def test_missing_file_is_empty_with_default_style(tmp_path):
    data, style = load(str(tmp_path / "nope.json"))
    assert data == {}
    assert style == FileStyle.default_for("nope.json")


@pytest.mark.parametrize("content", [b"", b"   \n\n", b"\xef\xbb\xbf"])
def test_empty_file_is_empty_dict(tmp_path, content):
    path = tmp_path / "fr.json"
    path.write_bytes(content)
    data, style = load(str(path))
    assert data == {}
    assert style.indent == "  " and style.trailing_newline is True


def test_invalid_json_raises_json_error(tmp_path):
    path = tmp_path / "fr.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load(str(path))


# --- Duplicates ---------------------------------------------------------------


def test_duplicate_keys_are_reported_and_last_wins(tmp_path):
    path = tmp_path / "fr.json"
    path.write_text(
        '{"a": "first", "b": {"c": 1, "c": 2}, "a": "second", "list": [{"x": 1, "x": 2}]}',
        encoding="utf-8",
    )
    data, style = load(str(path))
    assert data["a"] == "second"
    assert data["b"] == {"c": 2}
    assert list(data.keys()) == ["a", "b", "list"]
    assert sorted(style.duplicates) == sorted(["a", "b.c", "list[0].x"])


def test_no_duplicates_is_empty_list(tmp_path):
    path = tmp_path / "fr.json"
    path.write_text('{"a": {"b": 1}}', encoding="utf-8")
    assert load(str(path))[1].duplicates == []


# --- Atomic save ----------------------------------------------------------------


def test_atomic_save_leaves_no_temp_files(tmp_path):
    path = tmp_path / "fr.json"
    save(str(path), {"a": "x"})
    save(str(path), {"a": "y"})
    assert sorted(os.listdir(tmp_path)) == ["fr.json"]


def test_failed_save_keeps_original_and_cleans_up(tmp_path, monkeypatch):
    path = tmp_path / "fr.json"
    path.write_text('{"a": "x"}\n', encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(fileio.os, "replace", boom)
    with pytest.raises(OSError):
        save(str(path), {"a": "y"})
    assert path.read_text(encoding="utf-8") == '{"a": "x"}\n'
    assert sorted(os.listdir(tmp_path)) == ["fr.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_save_preserves_file_mode(tmp_path):
    path = tmp_path / "fr.json"
    path.write_text("{}", encoding="utf-8")
    os.chmod(str(path), 0o640)
    save(str(path), {"a": "x"})
    assert stat.S_IMODE(os.stat(str(path)).st_mode) == 0o640


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_new_file_gets_umask_mode_not_0600(tmp_path):
    path = tmp_path / "fr.json"
    save(str(path), {"a": "x"})
    umask = os.umask(0)
    os.umask(umask)
    assert stat.S_IMODE(os.stat(str(path)).st_mode) == 0o666 & ~umask


# --- ARB ------------------------------------------------------------------------


def test_arb_is_json_kind(tmp_path):
    style = FileStyle.default_for("app_fr.arb")
    assert style.kind == "json" and style.arb is True
    path = tmp_path / "app_fr.arb"
    path.write_text('{"@@locale": "fr", "hi": "Salut"}', encoding="utf-8")
    data, style = load(str(path))
    assert data == {"@@locale": "fr", "hi": "Salut"}
    assert style.arb is True


# --- Filenames ------------------------------------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("fr.json", "fr"),
    ("pt-BR.json", "pt-BR"),
    ("fr.backup.json", "fr.backup"),
    ("app_fr.arb", "app_fr"),
    ("de.yaml", "de"),
    ("de.YML", "de"),
    ("dir/ja.json", "ja"),
    ("notes.txt", None),
    ("fr", None),
    (".langsync-state.json", None),
    (".json", None),
])
def test_locale_from_filename(name, expected):
    assert locale_from_filename(name) == expected


def test_supported_extensions():
    assert SUPPORTED_EXTENSIONS == (".json", ".arb", ".yaml", ".yml")


# --- parse_text -----------------------------------------------------------------


def test_parse_text_json_and_empty():
    assert parse_text('﻿{"a": 1}', "en.json") == {"a": 1}
    assert parse_text("  ", "en.json") == {}
    with pytest.raises(json.JSONDecodeError):
        parse_text("nope", "en.json")


# --- YAML -----------------------------------------------------------------------


def _hide_yaml(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("No module named 'yaml'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setitem(sys.modules, "yaml", None)
    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_yaml_without_pyyaml_raises_format_error(tmp_path, monkeypatch):
    _hide_yaml(monkeypatch)
    path = tmp_path / "fr.yaml"
    path.write_text("a: b\n", encoding="utf-8")
    with pytest.raises(FormatError) as e:
        load(str(path))
    assert "pip install pyyaml" in str(e.value)
    assert "pipx inject langsync pyyaml" in str(e.value)
    with pytest.raises(FormatError):
        save(str(tmp_path / "de.yml"), {"a": "b"})


def test_empty_yaml_without_pyyaml_is_still_empty(tmp_path, monkeypatch):
    # `touch fr.yaml` should not demand PyYAML just to read nothing.
    _hide_yaml(monkeypatch)
    path = tmp_path / "fr.yaml"
    path.write_text("", encoding="utf-8")
    assert load(str(path))[0] == {}


def test_yaml_round_trip(tmp_path):
    pytest.importorskip("yaml")
    path = tmp_path / "fr.yaml"
    data = {"b": "Bonjour", "a": {"long": "x " * 200, "list": ["un", "deux"]}, "u": "日本"}
    save(str(path), data)
    loaded, style = load(str(path))
    assert loaded == data
    assert list(loaded.keys()) == ["b", "a", "u"]
    assert style.kind == "yaml"
    assert "日本" in path.read_text(encoding="utf-8")
    before = path.read_bytes()
    save(str(path), loaded, style)
    assert path.read_bytes() == before


def test_yaml_empty_and_syntax_error(tmp_path):
    pytest.importorskip("yaml")
    assert parse_text("", "fr.yaml") == {}
    assert parse_text("# just a comment\n", "fr.yml") == {}
    with pytest.raises(FormatError) as e:
        parse_text("a: [unclosed\n", "fr.yaml")
    assert "line" in str(e.value)
