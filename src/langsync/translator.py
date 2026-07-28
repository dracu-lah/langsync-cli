import time
import re
from deep_translator import GoogleTranslator
from . import icu
from .config import PLACEHOLDER_REGEX, LANG_MAP, WHITELIST as DEFAULT_WHITELIST


class TranslationError(Exception):
    """Raised when a translation request fails (network, rate limit, API error)."""
    def __init__(self, message, kind="unknown"):
        super().__init__(message)
        self.kind = kind  # "rate_limit", "network", "api", "icu", "unknown"


class TextProtector:
    @staticmethod
    def protect(text, whitelist=None, protect_hash=False):
        """Replaces whitelisted words and placeholders with unique markers.

        protect_hash=True additionally protects '#', the ICU plural number
        placeholder — used when the text is a fragment of a plural branch."""
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

        for word in sorted(active_whitelist, key=len, reverse=True):
            if not word:
                continue
            pattern = re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE)

            def word_replacer(match):
                marker = f"WL{len(markers)}X"
                markers[marker] = match.group(0)
                return marker

            protected_text = pattern.sub(word_replacer, protected_text)

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
            pattern = re.compile(re.escape(marker), re.IGNORECASE)
            # Replace via a callable so backslashes in the original text are
            # not interpreted as regex group references.
            restored_text = pattern.sub(lambda m, o=original: o, restored_text)

        return restored_text


def _classify_error(exc):
    msg = str(exc)
    if "429" in msg or "Too Many Requests" in msg.lower() or "rate" in msg.lower():
        return "rate_limit"
    if "timeout" in msg.lower() or "connection" in msg.lower() or "network" in msg.lower():
        return "network"
    return "api"


class TranslationService:
    def __init__(self, source_lang='en', target_lang='en', whitelist=None):
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.whitelist = whitelist
        self.translator = None
        if source_lang != target_lang:
            self.translator = GoogleTranslator(source=source_lang, target=target_lang)

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

    @staticmethod
    def _prepare_segment(raw, whitelist):
        """Split a span into (core, lead, trail) whitespace parts and protect
        the core. Returns (core, lead, trail, protected, markers)."""
        core = raw.strip()
        lead = raw[:len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()):]
        protected, markers = TextProtector.protect(core, whitelist, protect_hash=True)
        return core, lead, trail, protected, markers

    @staticmethod
    def _finish_segment(core, translated, markers):
        """Restore markers in a translated span fragment. Returns the restored
        text, or None when the translation is unusable (empty, or it lost an
        ICU '#' placeholder)."""
        if not translated:
            return None
        restored = TextProtector.restore(translated, markers)
        if not restored:
            return None
        # Google likes to append a period to sentence fragments.
        if not core.endswith('.'):
            restored = restored.rstrip('.')
        if not restored:
            return None
        if restored.count('#') != core.count('#'):
            return None
        return restored

    def translate_one(self, text, delay=0.0):
        """Translate a single string. Raises TranslationError on failure.
        Returns the original text if translation is not applicable (empty / too short / same lang).
        """
        if not self._needs_translation(text):
            return text

        parsed = self._structured(text)
        if parsed is not None:
            return self._translate_structured_one(text, parsed, delay)
        if icu.looks_structured(text):
            # Looks like a plural/select message but doesn't parse — the plain
            # marker path would expose its reserved keywords to the API.
            raise TranslationError(
                "string looks like ICU MessageFormat but does not parse; "
                "fix the source string", kind="icu",
            )

        protected_text, markers = TextProtector.protect(text, self.whitelist)
        try:
            translated = self.translator.translate(protected_text)
        except Exception as e:
            raise TranslationError(str(e), kind=_classify_error(e)) from e

        if delay > 0:
            time.sleep(delay)

        restored = TextProtector.restore(translated, markers)
        if not restored:
            raise TranslationError("Empty translation returned", kind="api")
        return restored

    def _translate_structured_one(self, text, parsed, delay):
        """Translate an ICU message by translating only its literal-text spans
        and splicing them back into the untouched structural skeleton. The
        result is re-parsed before being returned; anything unparseable raises
        instead of ever being handed back to a caller that might write it."""
        replacements = {}
        for idx, raw, _in_plural in parsed.translatable_spans():
            core, lead, trail, protected, markers = self._prepare_segment(raw, self.whitelist)
            try:
                translated = self.translator.translate(protected)
            except Exception as e:
                raise TranslationError(str(e), kind=_classify_error(e)) from e
            restored = self._finish_segment(core, translated, markers)
            if restored is None:
                raise TranslationError(
                    f"unusable translation for ICU segment {core!r}", kind="icu"
                )
            replacements[idx] = lead + restored + trail

        if delay > 0:
            time.sleep(delay)

        if not replacements:
            return text  # no human-readable text; the message is already fine

        result = parsed.rebuild(replacements)
        try:
            icu.parse(result)
        except icu.ICUParseError as e:
            raise TranslationError(
                f"translated ICU message failed validation ({e})", kind="icu"
            )
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

        ICU messages with plural/selectordinal/select arguments contribute only
        their literal-text spans to the API batch; the structural skeleton is
        reassembled locally and validated. An ICU item whose translation cannot
        be validated comes back as None — an invalid message is never returned.
        """
        if not self.translator or not texts:
            return texts

        # Build one flat list of protected strings to send. Plain texts map to
        # a single entry; structured ICU texts map to one entry per span.
        plans = []  # ("plain", flat_pos, markers) | ("icu", text, parsed, segments) | ("reject",)
        flat = []

        for text in texts:
            parsed = self._structured(text)
            if parsed is None and icu.looks_structured(text):
                plans.append(("reject",))
                continue
            if parsed is not None:
                segments = []
                for idx, raw, _in_plural in parsed.translatable_spans():
                    core, lead, trail, protected, markers = self._prepare_segment(raw, self.whitelist)
                    segments.append({
                        "idx": idx, "core": core, "lead": lead, "trail": trail,
                        "markers": markers, "flat_pos": len(flat),
                    })
                    flat.append(protected)
                plans.append(("icu", text, parsed, segments))
            else:
                has_trailing_dot = text.strip().endswith('.') if isinstance(text, str) else False
                protected_text, markers = TextProtector.protect(text, self.whitelist)
                markers['_meta'] = {'has_trailing_dot': has_trailing_dot}
                plans.append(("plain", len(flat), markers))
                flat.append(protected_text)

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
        for plan in plans:
            if plan[0] == "reject":
                results.append(None)
                continue
            if plan[0] == "plain":
                _, flat_pos, markers = plan
                translated = translated_flat[flat_pos]
                if translated is None or translated == "":
                    results.append(None)
                    continue
                restored = TextProtector.restore(translated, markers)
                if restored and not markers['_meta']['has_trailing_dot']:
                    restored = restored.rstrip('.')
                results.append(restored if restored else None)
                continue

            _, text, parsed, segments = plan
            replacements = {}
            usable = True
            for seg in segments:
                restored = self._finish_segment(
                    seg["core"], translated_flat[seg["flat_pos"]], seg["markers"]
                )
                if restored is None:
                    usable = False
                    break
                replacements[seg["idx"]] = seg["lead"] + restored + seg["trail"]
            if not usable:
                results.append(None)
                continue
            rebuilt = parsed.rebuild(replacements) if replacements else text
            # Belt and braces: never emit an ICU message that no longer parses.
            results.append(rebuilt if icu.is_valid(rebuilt) else None)
        return results


def get_translator_code(locale):
    lang_code = locale.split('-')[0]
    if lang_code in LANG_MAP:
        return LANG_MAP[lang_code](locale)
    return lang_code
