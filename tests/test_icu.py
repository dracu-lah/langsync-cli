import pytest

from langsync import icu
from langsync.icu import ICUParseError


# --- parsing: valid messages ------------------------------------------------

VALID = [
    "{count, plural, one {# seat map} other {# seat maps}}",
    "{count, plural, =0 {none} one {# item} other {# items}}",
    "{count, plural, offset:1 one {# other} other {# others}}",
    "{pos, selectordinal, one {#st} two {#nd} few {#rd} other {#th}}",
    "{gender, select, male {He} female {She} other {They}}",
    "Plain text without any arguments.",
    "Hello {name}, welcome back!",
    "{n, number, ::percent} done",
    "{when, date, short} at {when, time}",
    "It''s {count, plural, one {# thing} other {# things}}",
    "{a, plural, one {{b, select, x {inner} other {inner other}}} other {outer}}",
    "The same {aircraft} is fitted differently across "
    "{count, plural, one {# other airline} other {# other airlines}}. "
    "Line up the cabins before you pick a seat.",
]


@pytest.mark.parametrize("message", VALID)
def test_parse_valid(message):
    icu.parse(message)  # must not raise
    assert icu.is_valid(message)


# --- parsing: the corpus shapes from the incident must be rejected ----------

INVALID = [
    # translated 'other' keyword, Latin and non-Latin scripts
    "{count, plural, one {# seat map} andere {# seat maps}}",
    "{count, plural, one {# seat map} otro {# seat maps}}",
    "{count, plural, one {# seat map} その他 {# seat maps}}",
    "{count, plural, one {# seat map} أخرى {# seat maps}}",
    "{count, plural, one {# seat map} 其他 {# seat maps}}",
    # dropped 'other' branch
    "{count, plural, one {# seat map}}",
    "{gender, select, male {He} female {She}}",
    # structural mangling: trailing prose pulled inside the argument
    "Derselbe {aircraft} wird bei {count, plural, one {# airline} und {# airlines} angepasst}.",
    # broken braces
    "{count, plural, one {# seat map} other {# seat maps}",
    "Unmatched } brace",
    "{}",
    "{someArg,}",
    # offset misuse
    "{g, select, offset:1 male {He} other {They}}",
    "{count, plural, one {#} offset:1 other {#}}",
]


@pytest.mark.parametrize("message", INVALID)
def test_parse_invalid(message):
    with pytest.raises(ICUParseError):
        icu.parse(message)
    assert not icu.is_valid(message)


# --- span extraction --------------------------------------------------------

def test_spans_exclude_keywords_and_hash_stays_inside():
    msg = "{count, plural, one {# seat map} other {# seat maps}}"
    parsed = icu.parse(msg)
    assert parsed.has_branching
    spans = parsed.translatable_spans()
    raws = [raw for _, raw, _ in spans]
    assert raws == ["# seat map", "# seat maps"]
    # every span comes from inside a plural branch
    assert all(in_plural for _, _, in_plural in spans)
    # the skeleton (text outside spans) still holds the reserved tokens
    skeleton = parsed.rebuild({i: "" for i, _, _ in spans})
    assert "plural" in skeleton and "one" in skeleton and "other" in skeleton
    assert "seat" not in skeleton


def test_spans_cover_surrounding_prose_and_simple_args():
    msg = (
        "The same {aircraft} is fitted differently across "
        "{count, plural, one {# other airline} other {# other airlines}}. "
        "Line up the cabins."
    )
    parsed = icu.parse(msg)
    raws = [raw for _, raw, _ in parsed.translatable_spans()]
    # {aircraft} stays *inside* its span so markers can travel through translation
    assert raws[0] == "The same {aircraft} is fitted differently across "
    assert raws[1] == "# other airline"
    assert raws[2] == "# other airlines"
    assert raws[3] == ". Line up the cabins."


def test_plain_message_has_no_branching():
    parsed = icu.parse("Hello {name}, welcome!")
    assert not parsed.has_branching
    raws = [raw for _, raw, _ in parsed.translatable_spans()]
    assert raws == ["Hello {name}, welcome!"]


# --- rebuild ----------------------------------------------------------------

def test_rebuild_replaces_only_spans():
    msg = "{count, plural, one {# seat map} other {# seat maps}}"
    parsed = icu.parse(msg)
    replacements = {
        idx: raw.replace("seat map", "Sitzplan").replace("Sitzplans", "Sitzpläne")
        for idx, raw, _ in parsed.translatable_spans()
    }
    rebuilt = parsed.rebuild(replacements)
    assert "one {" in rebuilt and "other {" in rebuilt
    assert "Sitzplan" in rebuilt
    icu.parse(rebuilt)  # still valid


def test_rebuild_without_replacements_is_identity():
    msg = "{count, plural, one {# a} other {# b}}"
    parsed = icu.parse(msg)
    assert parsed.rebuild({}) == msg
