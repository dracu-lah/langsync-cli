import time
import re
from deep_translator import GoogleTranslator
from deep_translator.constants import (
    DEEPL_LANGUAGE_TO_CODE,
    LIBRE_LANGUAGES_TO_CODES,
    MY_MEMORY_LANGUAGES_TO_CODES,
)
from . import icu
from .config import PLACEHOLDER_REGEX, WHITELIST as DEFAULT_WHITELIST

try:  # The optional providers are looked up lazily so tests can patch them.
    from deep_translator import (
        DeeplTranslator,
        LibreTranslator,
        MicrosoftTranslator,
        MyMemoryTranslator,
    )
except ImportError:  # pragma: no cover - very old deep-translator
    DeeplTranslator = LibreTranslator = MicrosoftTranslator = MyMemoryTranslator = None

try:
    from deep_translator import exceptions as _dt_exc
except ImportError:  # pragma: no cover
    _dt_exc = None

try:
    import requests as _requests
except ImportError:  # pragma: no cover
    _requests = None


# Longest segment sent to the API in one piece. Google rejects 5000+ chars.
MAX_SEGMENT_CHARS = 4500

DEFAULT_LIBRE_URL = "https://libretranslate.com/"

# Every marker TextProtector emits: PH (placeholder / '#'), WL (whitelist),
# GL (glossary), followed by a counter and a closing 'X'.
_MARKER_RE = re.compile(r'(?:PH|WL|GL)\s?\d+\s?X', re.IGNORECASE)


class TranslationError(Exception):
    """Raised when a translation request fails (network, rate limit, API error)."""
    def __init__(self, message, kind="unknown"):
        super().__init__(message)
        # "rate_limit", "network", "api", "init", "icu", "placeholder", "unknown"
        self.kind = kind


def _marker_pattern(marker):
    """Regex for one marker, tolerant of the case changes and single inserted
    spaces translators sometimes apply ("PH0X" -> "ph0x", "PH 0 X")."""
    m = re.match(r'([A-Za-z]+)(\d+)X$', marker)
    if not m:
        return re.compile(re.escape(marker), re.IGNORECASE)
    return re.compile(
        re.escape(m.group(1)) + r'\s?' + m.group(2) + r'\s?X', re.IGNORECASE
    )


class TextProtector:
    @staticmethod
    def protect(text, whitelist=None, protect_hash=False, glossary=None):
        """Replaces whitelisted words, glossary terms and placeholders with
        unique markers.

        protect_hash=True additionally protects '#', the ICU plural number
        placeholder — used when the text is a fragment of a plural branch.
        Glossary terms are restored as their forced translation, whitelist
        terms as themselves. Both match case-sensitively on whole words."""
        if not isinstance(text, str):
            return text, {}

        markers = {}

        def placeholder_replacer(match):
            marker = f"PH{len(markers)}X"
            markers[marker] = match.group(0)
            return marker

        protected_text = PLACEHOLDER_REGEX.sub(placeholder_replacer, text)
        if protect_hash and "#" in protected_text:
            protected_text = re.sub(r"#", placeholder_replacer, protected_text)

        active_whitelist = whitelist if whitelist is not None else DEFAULT_WHITELIST
        terms = {}
        for word in active_whitelist:
            if word:
                terms[word] = ("WL", word)
        for term, forced in (glossary or {}).items():
            if term:
                terms[term] = ("GL", forced)  # a glossary entry wins over the whitelist

        # Longest first, so "Virgin Atlantic" is taken before "Virgin".
        for term in sorted(terms, key=len, reverse=True):
            prefix, replacement = terms[term]
            # (?<!\w)/(?!\w) instead of \b so terms that start or end with
            # punctuation ("Booking.com", "Inc.") still match.
            pattern = re.compile(r'(?<!\w)' + re.escape(term) + r'(?!\w)')

            def term_replacer(match, prefix=prefix, replacement=replacement):
                marker = f"{prefix}{len(markers)}X"
                markers[marker] = replacement
                return marker

            protected_text = pattern.sub(term_replacer, protected_text)

        return protected_text, markers

    @staticmethod
    def restore(text, markers):
        """Restores protected markers back to their original text."""
        if not text or not markers:
            return text

        restored_text = text
        for marker, original in sorted(markers.items(), key=lambda x: len(x[0]), reverse=True):
            if marker == '_meta':
                continue
            # Replace via a callable so backslashes in the original text are
            # not interpreted as regex group references.
            restored_text = _marker_pattern(marker).sub(lambda m, o=original: o, restored_text)

        return restored_text

    @staticmethod
    def markers_intact(text, markers):
        """True when every marker occurs in text exactly once — none lost by
        the translator, none duplicated."""
        for marker in markers:
            if marker == '_meta':
                continue
            if len(_marker_pattern(marker).findall(text or "")) != 1:
                return False
        return True


def _classify_error(exc):
    """Map a provider exception to a TranslationError kind."""
    if _dt_exc is not None:
        if isinstance(exc, _dt_exc.TooManyRequests):
            return "rate_limit"
        if isinstance(exc, (_dt_exc.LanguageNotSupportedException,
                            _dt_exc.InvalidSourceOrTargetLanguage,
                            _dt_exc.ApiKeyException,
                            _dt_exc.AuthorizationException)):
            return "init"
        if isinstance(exc, _dt_exc.RequestError):
            return "network"
        if isinstance(exc, _dt_exc.NotValidLength):
            return "api"
    if _requests is not None and isinstance(
        exc, (_requests.exceptions.ConnectionError, _requests.exceptions.Timeout)
    ):
        return "network"
    msg = str(exc).lower()
    if ("429" in msg or "too many requests" in msg or "rate limit" in msg
            or "ratelimit" in msg or "quota" in msg):
        return "rate_limit"
    if "timeout" in msg or "timed out" in msg or "connection" in msg or "network" in msg:
        return "network"
    return "api"


# --- language codes ----------------------------------------------------------

_TRADITIONAL_ZH_REGIONS = {"TW", "HK", "MO"}


def _split_locale(locale):
    """'zh_Hant_TW' -> ('zh', 'Hant', 'TW'). Missing parts are None."""
    parts = [p for p in str(locale).strip().replace('_', '-').split('-') if p]
    base = parts[0].lower() if parts else ""
    script = region = None
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha() and script is None:
            script = part.title()
        elif (len(part) == 2 and part.isalpha()) or (len(part) == 3 and part.isdigit()):
            if region is None:
                region = part.upper()
    return base, script, region


def _is_traditional_zh(script, region):
    return script == "Hant" or region in _TRADITIONAL_ZH_REGIONS


def _supported_lookup(codes):
    """{lowercased code: canonical code} for a provider's language table."""
    return {c.lower(): c for c in codes}


_GOOGLE_ALIASES = {"he": "iw", "nb": "no", "nn": "no", "fil": "tl", "jv": "jw", "mni": "mni-Mtei"}
_DEEPL_ALIASES = {"iw": "he", "no": "nb", "nn": "nb", "jw": "jv"}
_MS_ALIASES = {"iw": "he", "no": "nb", "nn": "nb", "tl": "fil", "jw": "jv", "mn": "mn-cyrl"}
_MYMEMORY_ALIASES = {"iw": "he", "no": "nb", "jw": "jv", "tl": "fil"}

_DEEPL_CODES = _supported_lookup(DEEPL_LANGUAGE_TO_CODE.values())
_LIBRE_CODES = _supported_lookup(LIBRE_LANGUAGES_TO_CODES.values())
_MYMEMORY_CODES = _supported_lookup(MY_MEMORY_LANGUAGES_TO_CODES.values())


def _resolve_from_table(base, script, region, table, aliases):
    """Pick the most specific code a provider table supports: base-Script-REGION,
    base-REGION, base-Script, then base."""
    base = aliases.get(base, base)
    candidates = []
    if script and region:
        candidates.append(f"{base}-{script}-{region}")
    if region:
        candidates.append(f"{base}-{region}")
    if script:
        candidates.append(f"{base}-{script}")
    candidates.append(base)
    for cand in candidates:
        if cand.lower() in table:
            return table[cand.lower()]
    return base


def resolve_language(locale, provider="google", overrides=None):
    """Translate a locale/file name ("pt_BR", "zh-Hant-TW", "nb-NO") into the
    language code the given provider expects. overrides (the config's
    "language_map") is consulted first, on the full locale then its base
    language, case-insensitively, and its value is returned unchanged."""
    if not locale or not isinstance(locale, str):
        return locale
    normalised = locale.strip().replace('_', '-')
    base, script, region = _split_locale(normalised)

    if overrides:
        lowered = {str(k).replace('_', '-').lower(): v for k, v in overrides.items()}
        for key in (normalised.lower(), base):
            if key in lowered:
                return lowered[key]

    provider = (provider or "google").lower()

    if provider == "deepl":
        if base == "zh":
            # Newer DeepL tables carry zh-Hans/zh-Hant; fall back to plain zh.
            want = "zh-Hant" if _is_traditional_zh(script, region) else "zh-Hans"
            return _DEEPL_CODES.get(want.lower(), "zh")
        return _resolve_from_table(base, None, region, _DEEPL_CODES, _DEEPL_ALIASES)

    if provider == "libre":
        if base == "zh":
            return "zt" if _is_traditional_zh(script, region) and "zt" in _LIBRE_CODES else "zh"
        return _resolve_from_table(base, None, None, _LIBRE_CODES, _DEEPL_ALIASES)

    if provider == "microsoft":
        # deep-translator lowercases Microsoft's codes ("zh-hans", "pt-pt").
        if base == "zh":
            return "zh-hant" if _is_traditional_zh(script, region) else "zh-hans"
        if base == "pt" and region == "PT":
            return "pt-pt"
        if base == "fr" and region == "CA":
            return "fr-ca"
        if base == "sr":
            return "sr-latn" if script == "Latn" else "sr-cyrl"
        return _MS_ALIASES.get(base, base)

    if provider == "mymemory":
        if base == "zh":
            if region in _TRADITIONAL_ZH_REGIONS:
                return f"zh-{region}"
            return "zh-TW" if script == "Hant" else "zh-CN"
        base = _MYMEMORY_ALIASES.get(base, base)
        found = _resolve_from_table(base, script, region, _MYMEMORY_CODES, {})
        if found != base:
            return found
        # MyMemory wants a region: prefer the "same letters" one (fr-FR), else
        # the first one its table lists for the language.
        if f"{base}-{base.upper()}".lower() in _MYMEMORY_CODES:
            return _MYMEMORY_CODES[f"{base}-{base.upper()}".lower()]
        for code in MY_MEMORY_LANGUAGES_TO_CODES.values():
            if code.split('-')[0].lower() == base:
                return code
        return base

    # google (default)
    if base == "zh":
        return "zh-TW" if _is_traditional_zh(script, region) else "zh-CN"
    return _GOOGLE_ALIASES.get(base, base)


def get_translator_code(locale):
    """Backward-compatible wrapper: the Google code for a locale."""
    return resolve_language(locale)


def select_glossary(glossary, locale):
    """Pick the {term: translation} entries for one target locale from the
    config's glossary ({"de": {...}, "pt-BR": {...}}). Entries for the base
    language apply first; an entry for the exact locale overrides them."""
    if not glossary or not locale:
        return {}
    normalised = str(locale).replace('_', '-').lower()
    base = normalised.split('-')[0]
    merged = {}
    by_key = {str(k).replace('_', '-').lower(): v for k, v in glossary.items()}
    for key in (base, normalised):
        entry = by_key.get(key)
        if isinstance(entry, dict):
            merged.update(entry)
    return merged


# --- segmentation ------------------------------------------------------------

def _has_letters(protected):
    return any(ch.isalpha() for ch in _MARKER_RE.sub("", protected))


def _split_long(text, limit=MAX_SEGMENT_CHARS):
    """Split text longer than limit into chunks at sentence boundaries, else at
    whitespace, else hard. Chunks concatenate back to text exactly."""
    chunks = []
    while len(text) > limit:
        window = text[:limit]
        cut = -1
        for sep in (". ", "! ", "? ", "。"):
            pos = window.rfind(sep)
            if pos > 0:
                cut = max(cut, pos + len(sep))
        if cut <= 0:
            m = None
            for m in re.finditer(r'\s+', window):
                pass
            if m is not None and m.end() > 0 and m.start() > 0:
                cut = m.end()
        if cut <= 0:
            cut = limit
            # Never cut a marker in half.
            for m in _MARKER_RE.finditer(text):
                if m.start() < cut < m.end():
                    cut = m.start() if m.start() > 0 else m.end()
                    break
        chunks.append(text[:cut])
        text = text[cut:]
    if text:
        chunks.append(text)
    return chunks


def _make_pieces(raw, whitelist, glossary, protect_hash=False):
    """Turn one span of text into assembly pieces: ("lit", text) kept verbatim,
    or ("seg", seg) to be translated. Leading/trailing whitespace is kept out
    of every segment and restored byte-for-byte."""
    core = raw.strip()
    lead = raw[:len(raw) - len(raw.lstrip())]
    trail = raw[len(raw.rstrip()):]
    if not core:
        return [("lit", raw)]
    protected, markers = TextProtector.protect(
        core, whitelist, protect_hash=protect_hash, glossary=glossary
    )
    if not _has_letters(protected):
        # Nothing a translator could change ("{count}", "100%", a URL).
        return [("lit", raw)]

    chunks = _split_long(protected) if len(protected) > MAX_SEGMENT_CHARS else [protected]
    pieces = [("lit", lead)] if lead else []
    for chunk in chunks:
        c_core = chunk.strip()
        c_lead = chunk[:len(chunk) - len(chunk.lstrip())]
        c_trail = chunk[len(chunk.rstrip()):]
        if not c_core:
            pieces.append(("lit", chunk))
            continue
        c_markers = {m: v for m, v in markers.items() if m in c_core}
        source = TextProtector.restore(c_core, c_markers) if c_markers else c_core
        if not _has_letters(c_core):
            pieces.append(("lit", c_lead + source + c_trail))
            continue
        pieces.append(("seg", {
            "lead": c_lead, "trail": c_trail, "protected": c_core,
            "markers": c_markers, "source": source,
        }))
    if trail:
        pieces.append(("lit", trail))
    return pieces


def _strip_added_period(protected_source, translated):
    """Google likes to append a period to fragments. Drop one when the source
    did not end with a period (or an ellipsis)."""
    if protected_source.endswith(('.', '。', '…')):
        return translated
    if translated.endswith(('..', '…')):
        return translated
    if translated.endswith(('.', '。')):
        return translated[:-1].rstrip()
    return translated


def _finish(seg, translated, check_hash=False):
    """Validate and restore one translated segment. Returns (text, None) or
    (None, reason) where reason is "empty" or "placeholder"."""
    if not isinstance(translated, str) or not translated.strip():
        return None, "empty"
    translated = _strip_added_period(seg["protected"], translated.strip())
    if not translated:
        return None, "empty"
    if not TextProtector.markers_intact(translated, seg["markers"]):
        return None, "placeholder"
    restored = TextProtector.restore(translated, seg["markers"])
    if not restored or not restored.strip():
        return None, "empty"
    if check_hash and restored.count('#') != seg["source"].count('#'):
        return None, "placeholder"
    return restored, None


def _assemble(pieces, finished):
    """Join pieces back together; finished maps id(seg) -> restored text."""
    out = []
    for kind, val in pieces:
        if kind == "lit":
            out.append(val)
        else:
            out.append(val["lead"] + finished[id(val)] + val["trail"])
    return "".join(out)


class TranslationService:
    def __init__(self, source_lang='auto', target_lang='en', whitelist=None,
                 provider='google', api_key=None, provider_url=None, glossary=None):
        self.source_lang = source_lang if source_lang else 'auto'
        self.target_lang = target_lang
        self.whitelist = whitelist
        self.provider = (provider or 'google').lower()
        self.api_key = api_key
        self.provider_url = provider_url
        self.glossary = dict(glossary) if glossary else {}
        self.translator = None
        if not self._same_language(self.source_lang, self.target_lang):
            self.translator = self._build_translator()

    @staticmethod
    def _same_language(source, target):
        """Same code, or same base language (en-GB vs en-US). Chinese scripts
        are different written languages, so zh-CN vs zh-TW still translates."""
        if not source or not target or source == 'auto':
            return False
        s, t = str(source).lower(), str(target).lower()
        if s == t:
            return True
        s_base, t_base = s.replace('_', '-').split('-')[0], t.replace('_', '-').split('-')[0]
        return s_base == t_base and s_base != 'zh'

    def _build_translator(self):
        source, target = self.source_lang, self.target_lang
        needs_key = {"deepl": "DeepL", "microsoft": "Microsoft Translator"}
        if self.provider in needs_key and not self.api_key:
            raise TranslationError(
                f"provider '{self.provider}' needs an API key: set \"api_key\" or "
                f"\"api_key_env\" in langsync.json", kind="init",
            )
        if self.provider == "libre" and not self.api_key and not self.provider_url:
            raise TranslationError(
                "provider 'libre' needs an API key for libretranslate.com, or "
                "\"provider_url\" pointing at your own LibreTranslate server", kind="init",
            )
        try:
            if self.provider == "google":
                return GoogleTranslator(source=source, target=target)
            if self.provider == "deepl":
                return DeeplTranslator(
                    api_key=self.api_key, source=source, target=target,
                    use_free_api=self.api_key.endswith(':fx'),
                )
            if self.provider == "libre":
                # A self-hosted server usually needs no key, but deep-translator
                # insists on one; the server ignores it when keys are disabled.
                return LibreTranslator(
                    api_key=self.api_key or "-", source=source, target=target,
                    use_free_api=False,
                    custom_url=self.provider_url or DEFAULT_LIBRE_URL,
                )
            if self.provider == "microsoft":
                return MicrosoftTranslator(api_key=self.api_key, source=source, target=target)
            if self.provider == "mymemory":
                return MyMemoryTranslator(source=source, target=target)
        except TranslationError:
            raise
        except Exception as e:
            raise TranslationError(str(e), kind="init") from e
        raise TranslationError(f"unknown provider '{self.provider}'", kind="init")

    def _needs_translation(self, text):
        return (
            self.translator is not None
            and isinstance(text, str)
            and text.strip()
            and len(text) >= 2
        )

    @staticmethod
    def _structured(text):
        """Return a ParsedMessage when text is valid ICU containing a
        plural/selectordinal/select argument, else None. Such messages must be
        translated span-by-span so the ICU skeleton never reaches the API."""
        if not isinstance(text, str) or "{" not in text:
            return None
        try:
            parsed = icu.parse(text)
        except icu.ICUParseError:
            return None
        return parsed if parsed.has_branching else None

    def _plan(self, text):
        """Work out how a string is translated. Returns one of
            ("keep",)                       - return text unchanged
            ("reject",)                     - ICU-looking but unparseable
            ("plain", pieces)               - line-by-line segments
            ("icu", parsed, [(idx, pieces)])
        """
        if not self._needs_translation(text):
            return ("keep",)
        parsed = self._structured(text)
        if parsed is not None:
            spans = []
            for idx, raw, _in_plural in parsed.translatable_spans():
                spans.append((idx, _make_pieces(raw, self.whitelist, self.glossary, protect_hash=True)))
            return ("icu", parsed, spans)
        if icu.looks_structured(text):
            # Looks like a plural/select message but doesn't parse — the plain
            # marker path would expose its reserved keywords to the API.
            return ("reject",)
        pieces = []
        # Each line is its own segment; "\r\n" separators stay intact.
        for i, part in enumerate(re.split(r'(\r?\n)', text)):
            if i % 2 or not part.strip():
                pieces.append(("lit", part))
            else:
                pieces.extend(_make_pieces(part, self.whitelist, self.glossary))
        return ("plain", pieces)

    @staticmethod
    def _segments_of(plan):
        if plan[0] == "plain":
            return [v for k, v in plan[1] if k == "seg"]
        if plan[0] == "icu":
            return [v for _, pieces in plan[2] for k, v in pieces if k == "seg"]
        return []

    def translate_one(self, text, delay=0.0):
        """Translate a single string. Raises TranslationError on failure.
        Returns the original text if translation is not applicable (empty / too short / same lang).
        """
        plan = self._plan(text)
        if plan[0] == "keep":
            return text
        if plan[0] == "reject":
            raise TranslationError(
                "string looks like ICU MessageFormat but does not parse; "
                "fix the source string", kind="icu",
            )

        segments = self._segments_of(plan)
        sent = {}
        for seg in segments:
            if seg["protected"] in sent:
                continue
            try:
                sent[seg["protected"]] = self.translator.translate(seg["protected"])
            except Exception as e:
                raise TranslationError(str(e), kind=_classify_error(e)) from e

        if delay > 0 and sent:
            time.sleep(delay)

        is_icu = plan[0] == "icu"
        finished = {}
        for seg in segments:
            restored, reason = _finish(seg, sent[seg["protected"]], check_hash=is_icu)
            if restored is None:
                if is_icu:
                    raise TranslationError(
                        f"unusable translation for ICU segment {seg['source']!r}", kind="icu"
                    )
                if reason == "placeholder":
                    raise TranslationError(
                        f"translation lost or duplicated a placeholder in {seg['source']!r}",
                        kind="placeholder",
                    )
                raise TranslationError("Empty translation returned", kind="api")
            finished[id(seg)] = restored

        if plan[0] == "plain":
            return _assemble(plan[1], finished)
        return self._rebuild_icu(text, plan, finished, raise_errors=True)

    @staticmethod
    def _rebuild_icu(text, plan, finished, raise_errors):
        """Splice translated spans into the ICU skeleton and re-validate.
        Invalid output raises (raise_errors) or returns None."""
        _, parsed, spans = plan
        replacements = {}
        for idx, pieces in spans:
            if any(k == "seg" for k, _ in pieces):
                replacements[idx] = _assemble(pieces, finished)
        if not replacements:
            return text  # no human-readable text; the message is already fine
        result = parsed.rebuild(replacements)
        # Belt and braces: never emit an ICU message that no longer parses.
        try:
            icu.parse(result)
        except icu.ICUParseError as e:
            if raise_errors:
                raise TranslationError(
                    f"translated ICU message failed validation ({e})", kind="icu"
                )
            return None
        return result

    def translate(self, text, delay=0.2):
        """Backwards-compatible: translates and silently returns the original on failure."""
        try:
            return self.translate_one(text, delay=delay)
        except TranslationError:
            return text

    def translate_batch(self, texts, delay=0.5):
        """Translates a list of strings. Raises TranslationError on full failure.
        Returns a list aligned with the input where each entry is either the translated
        string or None for items that came back empty/invalid.

        Every string is cut into segments (one per line, very long lines
        further split at sentence boundaries); identical segments are sent
        once. ICU messages with plural/selectordinal/select arguments
        contribute only their literal-text spans; the structural skeleton is
        reassembled locally and validated. An item whose translation lost a
        placeholder, or an ICU item that cannot be validated, comes back as
        None — a broken string is never returned.
        """
        if not self.translator or not texts:
            return texts

        plans = [self._plan(text) for text in texts]

        # One flat list of unique protected segments to send.
        flat = []
        flat_index = {}
        for plan in plans:
            for seg in self._segments_of(plan):
                if seg["protected"] not in flat_index:
                    flat_index[seg["protected"]] = len(flat)
                    flat.append(seg["protected"])

        translated_flat = []
        if flat:
            try:
                translated_flat = self.translator.translate_batch(flat)
            except Exception as e:
                kind = _classify_error(e)
                if kind == "rate_limit":
                    # Preserve legacy message so cli rate-limit branch keeps matching.
                    raise Exception("RATE_LIMIT_HIT") from e
                raise TranslationError(str(e), kind=kind) from e

            if delay > 0:
                time.sleep(delay)

            if not translated_flat or len(translated_flat) != len(flat):
                raise TranslationError(
                    f"Batch returned {len(translated_flat) if translated_flat else 0} items, expected {len(flat)}",
                    kind="api",
                )

        results = []
        for text, plan in zip(texts, plans):
            if plan[0] == "keep":
                results.append(text)
                continue
            if plan[0] == "reject":
                results.append(None)
                continue
            is_icu = plan[0] == "icu"
            finished = {}
            usable = True
            for seg in self._segments_of(plan):
                restored, _reason = _finish(
                    seg, translated_flat[flat_index[seg["protected"]]], check_hash=is_icu
                )
                if restored is None:
                    usable = False
                    break
                finished[id(seg)] = restored
            if not usable:
                results.append(None)
            elif is_icu:
                results.append(self._rebuild_icu(text, plan, finished, raise_errors=False))
            else:
                results.append(_assemble(plan[1], finished))
        return results
