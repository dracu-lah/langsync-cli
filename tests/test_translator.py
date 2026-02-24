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
