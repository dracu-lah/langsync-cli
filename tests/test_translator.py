import pytest
from langsync.translator import TextProtector

def test_protect_placeholders():
    text = "Hello {name}, welcome to <tag>our site</tag>."
    protected_text, markers = TextProtector.protect(text, whitelist=[])

    # The placeholder and each tag are protected on their own...
    assert "{name}" not in protected_text
    assert "<tag>" not in protected_text and "</tag>" not in protected_text
    values = set(markers.values())
    assert values == {"{name}", "<tag>", "</tag>"}
    # ...while the text between the tags stays visible to the translator.
    assert "our site" in protected_text
    assert TextProtector.restore(protected_text, markers) == text

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


def test_translate_batch_rejects_invalid_icu_looking_source(mocker):
    service = _make_service(mocker)
    service.translator.translate_batch.side_effect = lambda batch: [f"T({s})" for s in batch]
    bad = "{count, plural, one {# item}}"  # missing mandatory 'other'
    results = service.translate_batch([bad, "Fine."], delay=0)
    assert results[0] is None
    assert results[1] == "T(Fine.)"
    # The bad string never reached the API.
    sent = service.translator.translate_batch.call_args[0][0]
    assert all("plural" not in s for s in sent)


def test_translate_one_rejects_invalid_icu_looking_source(mocker):
    from langsync.translator import TranslationError
    service = _make_service(mocker)
    with pytest.raises(TranslationError) as exc_info:
        service.translate_one("{count, plural, one {# item}}", delay=0)
    assert exc_info.value.kind == "icu"
    service.translator.translate.assert_not_called()


# --- placeholder kinds ---------------------------------------------------------

@pytest.mark.parametrize("token", [
    "{{name}}", "{{- name}}", "{{count, number}}", "{name}", "{0}",
    "$t(common.ok)", '$t(items, {"count": 2})',
    "%s", "%d", "%i", "%f", "%@", "%1$s", "%2$d", "%.2f", "%%",
    "%{name}", "${user}", ":name",
    "&amp;", "&#39;", "&#x27;", "&nbsp;",
    "https://example.com/a?b=1", "help@example.com",
    "<b>", "</b>", "<br/>", "<br />", '<a href="/terms">', "<0>", "</0>",
])
def test_placeholder_kinds_protected_and_restored(token):
    text = f"Before {token} after"
    protected, markers = TextProtector.protect(text, whitelist=[])
    assert token not in protected
    assert token in markers.values()
    assert TextProtector.restore(protected, markers) == text


@pytest.mark.parametrize("text", [
    "a < b and c > d", "Meet at 10:30", "100% sure", "Note: read this",
])
def test_prose_is_not_mistaken_for_placeholders(text):
    protected, markers = TextProtector.protect(text, whitelist=[])
    assert markers == {}
    assert protected == text


def test_i18next_double_brace_is_one_token():
    protected, markers = TextProtector.protect("Hi {{name}}!", whitelist=[])
    assert list(markers.values()) == ["{{name}}"]


def test_url_excludes_trailing_period():
    protected, markers = TextProtector.protect("See https://x.com/docs.", whitelist=[])
    assert "https://x.com/docs" in markers.values()
    assert protected.endswith(".")


# --- whitelist and glossary --------------------------------------------------

def test_whitelist_is_case_sensitive():
    protected, markers = TextProtector.protect("Pro tips for PRO users", whitelist=["PRO"])
    assert "Pro tips" in protected
    assert list(markers.values()) == ["PRO"]


def test_whitelist_term_with_punctuation():
    text = "Book on Booking.com today, via Acme Inc. now"
    protected, markers = TextProtector.protect(text, whitelist=["Booking.com", "Inc."])
    assert "Booking.com" not in protected and "Inc." not in protected
    assert TextProtector.restore(protected, markers) == text


def test_whitelist_does_not_match_inside_words():
    protected, markers = TextProtector.protect("Kiwis and Kiwi", whitelist=["Kiwi"])
    assert protected.startswith("Kiwis")
    assert len(markers) == 1


def test_default_whitelist_is_empty():
    protected, markers = TextProtector.protect("Upgrade to PRO with Kiwi", whitelist=None)
    assert markers == {}


def test_glossary_forces_translation(mocker):
    from langsync.translator import TranslationService
    mocker.patch("langsync.translator.GoogleTranslator")
    service = TranslationService(source_lang="en", target_lang="de", whitelist=[],
                                 glossary={"Seat map": "Sitzplan"})
    sent = []

    def fake(batch):
        sent.extend(batch)
        return [s.replace("Open the", "Öffne den") for s in batch]

    service.translator.translate_batch.side_effect = fake
    out = service.translate_batch(["Open the Seat map"], delay=0)
    assert "Seat map" not in sent[0]
    assert out == ["Öffne den Sitzplan"]


def test_select_glossary_merges_base_and_locale():
    from langsync.translator import select_glossary
    glossary = {"pt": {"Save": "Salvar", "Cart": "Carrinho"}, "pt-PT": {"Save": "Guardar"}}
    assert select_glossary(glossary, "pt_PT") == {"Save": "Guardar", "Cart": "Carrinho"}
    assert select_glossary(glossary, "pt-BR") == {"Save": "Salvar", "Cart": "Carrinho"}
    assert select_glossary(glossary, "de") == {}
    assert select_glossary(None, "de") == {}


# --- segments ------------------------------------------------------------------

def _echo_service(mocker, fn=None):
    from langsync.translator import TranslationService
    mocker.patch("langsync.translator.GoogleTranslator")
    service = TranslationService(source_lang="en", target_lang="de", whitelist=[])
    sent = []

    def fake(batch):
        sent.append(list(batch))
        return [fn(s) if fn else f"T({s})" for s in batch]

    service.translator.translate_batch.side_effect = fake
    service.translator.translate.side_effect = lambda s: fn(s) if fn else f"T({s})"
    return service, sent


def test_tags_individually_protected_inner_text_translated(mocker):
    service, sent = _echo_service(mocker, lambda s: s.replace("here", "hier"))
    out = service.translate_batch(["Click <b>here</b> now"], delay=0)
    assert "here" in sent[0][0]
    assert out == ["Click <b>hier</b> now"]


def test_lost_marker_gives_none(mocker):
    service, _ = _echo_service(mocker, lambda s: "Hallo Welt")
    assert service.translate_batch(["Hello {name} world"], delay=0) == [None]


def test_duplicated_marker_gives_none(mocker):
    service, _ = _echo_service(mocker, lambda s: s + " " + s.split()[1])
    assert service.translate_batch(["Hello {name} world"], delay=0) == [None]


def test_lowercased_marker_is_tolerated(mocker):
    service, _ = _echo_service(mocker, lambda s: s.lower())
    assert service.translate_batch(["Hello {name}"], delay=0) == ["hello {name}"]


def test_translate_one_lost_marker_raises_placeholder(mocker):
    from langsync.translator import TranslationError
    service, _ = _echo_service(mocker, lambda s: "Hallo")
    with pytest.raises(TranslationError) as exc_info:
        service.translate_one("Hello {name}", delay=0)
    assert exc_info.value.kind == "placeholder"


def test_newlines_and_whitespace_preserved(mocker):
    service, sent = _echo_service(mocker)
    text = "  First line.\r\n\nSecond line  \n   \n\tThird"
    out = service.translate_batch([text], delay=0)[0]
    assert out == "  T(First line.)\r\n\nT(Second line)  \n   \n\tT(Third)"
    assert sent[0] == ["First line.", "Second line", "Third"]


def test_segments_without_letters_not_sent(mocker):
    service, sent = _echo_service(mocker)
    out = service.translate_batch(["{count}", "100%", "https://x.com/a", "—", "Hi {count}"], delay=0)
    assert out == ["{count}", "100%", "https://x.com/a", "—", "T(Hi {count})"]
    assert sent == [["Hi PH0X"]]


def test_no_api_call_when_nothing_to_translate(mocker):
    service, sent = _echo_service(mocker)
    assert service.translate_batch(["{count}", "x", ""], delay=0) == ["{count}", "x", ""]
    assert sent == []


def test_long_string_is_chunked_and_rejoined(mocker):
    import langsync.translator as tr
    service, _ = _echo_service(mocker)
    text = " ".join(f"Sentence number {i} is fairly ordinary." for i in range(300))
    calls = []

    def fake(s):
        calls.append(s)
        assert len(s) <= tr.MAX_SEGMENT_CHARS
        return s.upper()

    service.translator.translate.side_effect = fake
    out = service.translate_one(text, delay=0)
    assert len(calls) >= 3
    assert out == text.upper()


def test_long_string_without_sentences_splits_on_whitespace():
    from langsync.translator import _split_long
    text = "word " * 2000
    chunks = _split_long(text, limit=100)
    assert "".join(chunks) == text
    assert all(len(c) <= 100 for c in chunks)


def test_duplicate_segments_sent_once(mocker):
    service, sent = _echo_service(mocker)
    out = service.translate_batch(["Save", "Cancel", "Save", "Save\nCancel"], delay=0)
    assert out == ["T(Save)", "T(Cancel)", "T(Save)", "T(Save)\nT(Cancel)"]
    assert sent == [["Save", "Cancel"]]


def test_identical_translation_is_valid(mocker):
    service, _ = _echo_service(mocker, lambda s: s)
    assert service.translate_batch(["OK", "Email"], delay=0) == ["OK", "Email"]
    assert service.translate_one("OK", delay=0) == "OK"


def test_appended_period_stripped(mocker):
    service, _ = _echo_service(mocker, lambda s: s + ("" if s.endswith((".", "…")) else "."))
    assert service.translate_batch(["Settings", "Done.", "Loading…"], delay=0) == [
        "Settings", "Done.", "Loading…",
    ]


def test_appended_cjk_period_stripped(mocker):
    service, _ = _echo_service(mocker, lambda s: "设置。")
    assert service.translate_batch(["Settings"], delay=0) == ["设置"]


def test_ellipsis_in_translation_kept(mocker):
    service, _ = _echo_service(mocker, lambda s: "Laden...")
    assert service.translate_batch(["Loading"], delay=0) == ["Laden..."]


# --- providers and language codes ------------------------------------------------

@pytest.mark.parametrize("locale,expected", [
    ("pt_BR", "pt"), ("pt-PT", "pt"), ("zh-TW", "zh-TW"), ("zh-HK", "zh-TW"),
    ("zh-MO", "zh-TW"), ("zh-Hant", "zh-TW"), ("zh_Hant_HK", "zh-TW"),
    ("zh", "zh-CN"), ("zh-SG", "zh-CN"), ("zh-Hans", "zh-CN"),
    ("he-IL", "iw"), ("nb-NO", "no"), ("nn", "no"), ("fil-PH", "tl"),
    ("jv", "jw"), ("mni", "mni-Mtei"), ("EN-gb", "en"), ("fr_CA", "fr"),
])
def test_resolve_language_google(locale, expected):
    from langsync.translator import resolve_language
    assert resolve_language(locale) == expected


def test_resolve_language_overrides():
    from langsync.translator import resolve_language
    overrides = {"nb-NO": "nb", "PT": "pt-xx"}
    assert resolve_language("nb_no", overrides=overrides) == "nb"
    assert resolve_language("pt-BR", overrides=overrides) == "pt-xx"
    assert resolve_language("de-DE", overrides=overrides) == "de"


@pytest.mark.parametrize("provider,locale,expected", [
    ("deepl", "en-GB", "en"), ("deepl", "pt-BR", "pt"), ("deepl", "zh-TW", "zh"),
    ("deepl", "de-DE", "de"),
    ("microsoft", "zh-TW", "zh-hant"), ("microsoft", "zh-CN", "zh-hans"),
    ("microsoft", "pt-PT", "pt-pt"), ("microsoft", "pt-BR", "pt"),
    ("microsoft", "he-IL", "he"), ("microsoft", "sr-Latn", "sr-latn"),
    ("mymemory", "fr", "fr-FR"), ("mymemory", "fr-CA", "fr-CA"),
    ("mymemory", "pt_BR", "pt-BR"), ("mymemory", "zh-HK", "zh-HK"), ("mymemory", "zh", "zh-CN"),
    ("libre", "pt-BR", "pt"), ("libre", "zh-TW", "zh"),
])
def test_resolve_language_other_providers(provider, locale, expected):
    from langsync.translator import resolve_language
    assert resolve_language(locale, provider=provider) == expected


def test_same_base_language_is_copy_through():
    from langsync.translator import TranslationService
    assert TranslationService(source_lang="en", target_lang="en").translator is None
    assert TranslationService(source_lang="en-GB", target_lang="en").translator is None
    assert TranslationService(source_lang=None, target_lang="en").source_lang == "auto"


def test_chinese_variants_are_not_copy_through(mocker):
    from langsync.translator import TranslationService
    mocker.patch("langsync.translator.GoogleTranslator")
    assert TranslationService(source_lang="zh-CN", target_lang="zh-TW").translator is not None


@pytest.mark.parametrize("provider,cls,key,kwargs", [
    ("deepl", "DeeplTranslator", "abc:fx", {"use_free_api": True}),
    ("deepl", "DeeplTranslator", "abc", {"use_free_api": False}),
    ("microsoft", "MicrosoftTranslator", "k", {}),
    ("mymemory", "MyMemoryTranslator", None, {}),
    ("libre", "LibreTranslator", "k", {"custom_url": "https://libretranslate.com/"}),
])
def test_provider_factory(mocker, provider, cls, key, kwargs):
    from langsync.translator import TranslationService
    mock_cls = mocker.patch(f"langsync.translator.{cls}")
    service = TranslationService(source_lang="en", target_lang="de", provider=provider, api_key=key)
    assert service.translator is mock_cls.return_value
    _, called = mock_cls.call_args
    assert called["source"] == "en" and called["target"] == "de"
    for k, v in kwargs.items():
        assert called[k] == v


def test_libre_custom_url_without_key(mocker):
    from langsync.translator import TranslationService
    mock_cls = mocker.patch("langsync.translator.LibreTranslator")
    TranslationService(source_lang="en", target_lang="de", provider="libre",
                       provider_url="http://localhost:5000/")
    assert mock_cls.call_args[1]["custom_url"] == "http://localhost:5000/"


@pytest.mark.parametrize("provider", ["deepl", "microsoft", "libre"])
def test_provider_missing_key_raises_init(mocker, provider):
    from langsync.translator import TranslationService, TranslationError
    with pytest.raises(TranslationError) as exc_info:
        TranslationService(source_lang="en", target_lang="de", provider=provider)
    assert exc_info.value.kind == "init"
    assert "api" in str(exc_info.value).lower()


def test_unsupported_language_raises_init():
    from langsync.translator import TranslationService, TranslationError
    with pytest.raises(TranslationError) as exc_info:
        TranslationService(source_lang="en", target_lang="xx-nonsense")
    assert exc_info.value.kind == "init"


# --- error classification ------------------------------------------------------

def test_classify_error():
    from langsync.translator import _classify_error
    from deep_translator import exceptions as ex
    assert _classify_error(Exception("429 Too Many Requests")) == "rate_limit"
    assert _classify_error(Exception("Server Error: Too Many Requests")) == "rate_limit"
    assert _classify_error(ex.TooManyRequests()) == "rate_limit"
    assert _classify_error(ex.RequestError()) == "network"
    assert _classify_error(ex.LanguageNotSupportedException("xx")) == "init"
    assert _classify_error(ex.NotValidLength("x", 0, 5000)) == "api"
    assert _classify_error(Exception("Connection reset")) == "network"
    assert _classify_error(Exception("accurate translation failed")) == "api"


def test_batch_api_error_raises_translation_error(mocker):
    from langsync.translator import TranslationError
    service, _ = _echo_service(mocker)
    service.translator.translate_batch.side_effect = Exception("boom")
    with pytest.raises(TranslationError) as exc_info:
        service.translate_batch(["Hello"], delay=0)
    assert exc_info.value.kind == "api"
