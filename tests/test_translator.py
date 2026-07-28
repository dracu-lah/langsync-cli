import pytest
from langsync.translator import TextProtector

def test_protect_placeholders():
    text = "Hello {name}, welcome to <tag>our site</tag>."
    protected_text, markers = TextProtector.protect(text, whitelist=[])
    
    # Check that placeholders are replaced by markers
    assert "PH0X" in protected_text
    assert "PH1X" in protected_text
    assert "{name}" not in protected_text
    assert "<tag>our site</tag>" not in protected_text
    
    # Verify markers mapping
    # Note: PH0X and PH1X order depends on regex match order
    # Let's check markers values
    values = set(markers.values())
    assert "{name}" in values
    assert "<tag>our site</tag>" in values

def test_protect_whitelist():
    text = "I love using Lascade and Arch Linux."
    whitelist = ["Lascade", "Arch Linux"]
    protected_text, markers = TextProtector.protect(text, whitelist=whitelist)
    
    assert "WL0X" in protected_text or "WL1X" in protected_text
    assert "Lascade" not in protected_text
    assert "Arch Linux" not in protected_text
    
    values = set(markers.values())
    assert "Lascade" in values
    assert "Arch Linux" in values

def test_restore():
    original_text = "Hello {name}, welcome to Lascade."
    whitelist = ["Lascade"]
    protected_text, markers = TextProtector.protect(original_text, whitelist=whitelist)
    
    # Simulate translation that might change case or add spaces (though restore should handle it)
    translated_text = protected_text.replace("WL", "wl") # Test case-insensitivity
    
    restored_text = TextProtector.restore(translated_text, markers)
    assert restored_text == original_text

def test_protect_nested_overlap():
    # Longest whitelist words should be protected first to avoid partial matches
    text = "Visit Virgin Atlantic for Virgin Points."
    whitelist = ["Virgin Atlantic", "Virgin Points", "Virgin"]
    protected_text, markers = TextProtector.protect(text, whitelist=whitelist)
    
    # Should not have "Virgin" partially replaced inside "Virgin Atlantic"
    # Wait, the code uses regex with \b (word boundary), so partial word matches are avoided.
    # But it sorts whitelist by length descending.
    
    restored = TextProtector.restore(protected_text, markers)
    assert restored == text

def test_restore_robustness():
    text = "PH0X is a marker."
    markers = {"PH0X": "{name}"}
    
    # Translator might add spaces around markers
    translated = " PH0X  is a marker."
    restored = TextProtector.restore(translated, markers)
    assert restored == " {name}  is a marker."

def test_get_translator_code():
    from langsync.translator import get_translator_code
    assert get_translator_code("zh-CN") == "zh-CN"
    assert get_translator_code("zh-TW") == "zh-TW"
    assert get_translator_code("nb-NO") == "no"
    assert get_translator_code("he-IL") == "iw"
    assert get_translator_code("en-GB") == "en"

def test_translation_service_init():
    from langsync.translator import TranslationService
    service = TranslationService(source_lang="en", target_lang="es")
    assert service.source_lang == "en"
    assert service.target_lang == "es"
    assert service.translator is not None

    service_same = TranslationService(source_lang="en", target_lang="en")
    assert service_same.translator is None

def test_translate_batch(mocker):
    from langsync.translator import TranslationService
    mock_translator_class = mocker.patch("langsync.translator.GoogleTranslator")
    mock_instance = mock_translator_class.return_value
    mock_instance.translate_batch.return_value = ["Hola", "Mundo"]

    service = TranslationService(source_lang="en", target_lang="es")
    results = service.translate_batch(["Hello", "World"], delay=0)
    
    assert results == ["Hola", "Mundo"]
    mock_instance.translate_batch.assert_called_once()

def test_translate_batch_rate_limit(mocker):
    from langsync.translator import TranslationService
    mock_translator_class = mocker.patch("langsync.translator.GoogleTranslator")
    mock_instance = mock_translator_class.return_value
    mock_instance.translate_batch.side_effect = Exception("429 Too Many Requests")

    service = TranslationService(source_lang="en", target_lang="es")
    with pytest.raises(Exception, match="RATE_LIMIT_HIT"):
        service.translate_batch(["Hello"], delay=0)


# --- ICU MessageFormat safety ------------------------------------------------

ICU_PLURAL = "{count, plural, one {# seat map} other {# seat maps}}"


def _make_service(mocker):
    from langsync.translator import TranslationService
    mocker.patch("langsync.translator.GoogleTranslator")
    return TranslationService(source_lang="en", target_lang="de", whitelist=[])


def test_translate_batch_icu_keywords_never_sent_to_api(mocker):
    service = _make_service(mocker)
    sent = []

    def fake_batch(batch):
        sent.extend(batch)
        return [f"T({s})" for s in batch]

    service.translator.translate_batch.side_effect = fake_batch
    results = service.translate_batch([ICU_PLURAL], delay=0)

    # Reserved tokens never leave the process.
    for item in sent:
        assert "plural" not in item
        assert "other" not in item
        assert "{" not in item and "}" not in item and "#" not in item

    out = results[0]
    assert out is not None
    # Structure intact, branch bodies translated.
    assert "one {" in out and "other {" in out
    assert out.count("#") == 2
    assert "T(" in out
    from langsync import icu
    icu.parse(out)


def test_translate_batch_mixed_plain_and_icu(mocker):
    service = _make_service(mocker)
    service.translator.translate_batch.side_effect = lambda batch: [f"T({s})" for s in batch]
    results = service.translate_batch(["Hello world.", ICU_PLURAL, "Goodbye."], delay=0)
    assert len(results) == 3
    assert results[0] == "T(Hello world.)"
    assert "other {" in results[1]
    assert results[2] == "T(Goodbye.)"


def test_translate_batch_rejects_structure_breaking_translation(mocker):
    service = _make_service(mocker)
    # Simulate the API mangling a fragment into something with a stray brace.
    service.translator.translate_batch.side_effect = (
        lambda batch: ["kaputt } kaputt" for _ in batch]
    )
    results = service.translate_batch([ICU_PLURAL], delay=0)
    assert results == [None]  # rejected, never emitted


def test_translate_batch_rejects_dropped_hash(mocker):
    service = _make_service(mocker)

    def drop_marker(batch):
        # Lose the protected '#' marker entirely, keep some text.
        return ["Sitzplan" for _ in batch]

    service.translator.translate_batch.side_effect = drop_marker
    results = service.translate_batch([ICU_PLURAL], delay=0)
    assert results == [None]


def test_translate_one_structured(mocker):
    service = _make_service(mocker)
    service.translator.translate.side_effect = lambda s: f"T({s})"
    out = service.translate_one(ICU_PLURAL, delay=0)
    assert "one {" in out and "other {" in out
    assert out.count("#") == 2
    from langsync import icu
    icu.parse(out)


def test_translate_one_structured_failure_raises(mocker):
    from langsync.translator import TranslationError
    service = _make_service(mocker)
    service.translator.translate.side_effect = lambda s: "junk } junk"
    with pytest.raises(TranslationError) as exc_info:
        service.translate_one(ICU_PLURAL, delay=0)
    assert exc_info.value.kind == "icu"


def test_translate_silent_fallback_keeps_source_on_icu_failure(mocker):
    service = _make_service(mocker)
    service.translator.translate.side_effect = lambda s: "junk } junk"
    # translate() (the silent variant) must fall back to the source, never
    # return a corrupted message.
    assert service.translate(ICU_PLURAL, delay=0) == ICU_PLURAL


def test_translate_batch_trailing_prose_stays_outside_argument(mocker):
    service = _make_service(mocker)
    service.translator.translate_batch.side_effect = lambda batch: [f"T({s})" for s in batch]
    msg = (
        "The same {aircraft} is fitted differently across "
        "{count, plural, one {# other airline} other {# other airlines}}. "
        "Line up the cabins."
    )
    out = service.translate_batch([msg], delay=0)[0]
    from langsync import icu
    parsed = icu.parse(out)
    # The sentence after the plural must still be *outside* the argument.
    assert out.rstrip().endswith("Line up the cabins.)")
    assert "{aircraft}" in out
    assert "other {" in out


def test_invalid_icu_falls_back_to_plain_path(mocker):
    # A message with braces that is NOT valid ICU goes through the legacy
    # marker-based path instead of being rejected.
    service = _make_service(mocker)
    service.translator.translate_batch.side_effect = lambda batch: [f"T({s})" for s in batch]
    out = service.translate_batch(["broken { but translatable"], delay=0)[0]
    assert out is not None and "T(" in out


def test_restore_with_backslashes():
    from langsync.translator import TextProtector
    text = r"Path {value} with \1 backslash"
    protected, markers = TextProtector.protect(text, whitelist=[])
    assert TextProtector.restore(protected, markers) == text
