import time
import re
from deep_translator import GoogleTranslator
from .config import PLACEHOLDER_REGEX, LANG_MAP, WHITELIST as DEFAULT_WHITELIST


class TranslationError(Exception):
    """Raised when a translation request fails (network, rate limit, API error)."""
    def __init__(self, message, kind="unknown"):
        super().__init__(message)
        self.kind = kind  # "rate_limit", "network", "api", "unknown"


class TextProtector:
    @staticmethod
    def protect(text, whitelist=None):
        """Replaces whitelisted words and placeholders with unique markers."""
        if not isinstance(text, str):
            return text, {}

        markers = {}

        def placeholder_replacer(match):
            marker = f"PH{len(markers)}X"
            markers[marker] = match.group(0)
            return marker

        protected_text = PLACEHOLDER_REGEX.sub(placeholder_replacer, text)

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
            restored_text = pattern.sub(original, restored_text)

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

    def translate_one(self, text, delay=0.0):
        """Translate a single string. Raises TranslationError on failure.
        Returns the original text if translation is not applicable (empty / too short / same lang).
        """
        if not self._needs_translation(text):
            return text

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
        """
        if not self.translator or not texts:
            return texts

        protected_batch = []
        batch_markers = []

        for text in texts:
            has_trailing_dot = text.strip().endswith('.') if isinstance(text, str) else False
            protected_text, markers = TextProtector.protect(text, self.whitelist)
            protected_batch.append(protected_text)
            markers['_meta'] = {'has_trailing_dot': has_trailing_dot}
            batch_markers.append(markers)

        try:
            translated_batch = self.translator.translate_batch(protected_batch)
        except Exception as e:
            kind = _classify_error(e)
            if kind == "rate_limit":
                # Preserve legacy message so cli rate-limit branch keeps matching.
                raise Exception("RATE_LIMIT_HIT") from e
            raise TranslationError(str(e), kind=kind) from e

        if delay > 0:
            time.sleep(delay)

        if not translated_batch or len(translated_batch) != len(texts):
            raise TranslationError(
                f"Batch returned {len(translated_batch) if translated_batch else 0} items, expected {len(texts)}",
                kind="api",
            )

        results = []
        for translated, markers in zip(translated_batch, batch_markers):
            if translated is None or translated == "":
                results.append(None)
                continue
            restored = TextProtector.restore(translated, markers)
            if restored and not markers['_meta']['has_trailing_dot']:
                restored = restored.rstrip('.')
            results.append(restored if restored else None)
        return results


def get_translator_code(locale):
    lang_code = locale.split('-')[0]
    if lang_code in LANG_MAP:
        return LANG_MAP[lang_code](locale)
    return lang_code
