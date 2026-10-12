from collections import Counter

import pytest

from langsync.validate import check_translation, extract_placeholders


def test_extract_placeholders_variables_and_tags():
    found = extract_placeholders(
        'Hi {{name}}, you have %d items. <b>Visit</b> https://x.com or mail a@b.co &amp; {0}'
    )
    assert found == Counter({"{{name}}": 1, "%d": 1, "<b>": 1, "</b>": 1, "{0}": 1})


def test_extract_placeholders_icu_argument_names():
    found = extract_placeholders(
        "{count, plural, one {# item for {name}} other {# items for {name}}}"
    )
    assert found == Counter({"{count}": 1, "{name}": 1})


def test_extract_placeholders_non_string():
    assert extract_placeholders(None) == Counter()
    assert extract_placeholders(3) == Counter()


@pytest.mark.parametrize("source,target", [
    ("Hello {name}", "Hallo {name}"),
    ("{a} and {b}", "{b} und {a}"),                      # reordered
    ("Hi {{ name }}", "Hallo {{name}}"),                   # brace spacing
    ('<a href="/x">Terms</a>', '<a href="/y">Bedingungen</a>'),  # attributes differ
    ("Line<br/>break", "Zeile<br>Umbruch"),
    ("See https://x.com/en", "Siehe https://x.com/de"),    # localised URL
    ("Tom &amp; Jerry", "Tom & Jerry"),
    ("Save", "Save"),                                      # identical is fine
    ("", ""),
    ("{count, plural, one {# item} other {# items}}",
     "{count, plural, other {# Artikel}}"),                # branches dropped
    ("{count, plural, one {# item} other {# items}}",
     "{count, plural, one {# plik} few {# pliki} many {# plików} other {# pliku}}"),
    ("{count, plural, one {item} other {items}}",
     "{count, plural, one {Artikel} other {Artikel}}"),    # branch text is not an argument
])
def test_check_translation_ok(source, target):
    assert check_translation(source, target) == []


def test_check_translation_missing_and_unexpected():
    assert check_translation("Hello {name}", "Hallo {nmae}") == [
        "missing placeholder {name}", "unexpected placeholder {nmae}",
    ]
    assert check_translation("%s of %d", "%s von") == ["missing placeholder %d"]


def test_check_translation_tag_mismatch():
    assert check_translation("Click <b>here</b>", "Klick hier") == [
        "tag mismatch </b>", "tag mismatch <b>",
    ]
    assert "tag mismatch </0>" in check_translation("<0>Hi</0>", "<0>Hallo")


def test_check_translation_empty():
    assert check_translation("Hello", "  ") == ["empty translation"]


def test_check_translation_invalid_icu():
    src = "{count, plural, one {# item} other {# items}}"
    assert check_translation(src, "{count, plural, one {# Artikel} other {# Artikel}") == ["invalid ICU"]


def test_check_translation_icu_missing_argument():
    src = "{count, plural, one {# item for {name}} other {# items for {name}}}"
    tgt = "{count, plural, one {# Artikel} other {# Artikel}}"
    assert check_translation(src, tgt) == ["missing placeholder {name}"]


def test_check_translation_non_strings():
    assert check_translation(None, "x") == []
    assert check_translation("x", 5) == []
    assert check_translation(["a"], ["b"]) == []
