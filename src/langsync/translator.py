import time
import re
from deep_translator import GoogleTranslator
from .config import PLACEHOLDER_REGEX, LANG_MAP, WHITELIST as DEFAULT_WHITELIST

class TextProtector:
    @staticmethod
    def protect(text, whitelist=None):
        """Replaces whitelisted words and placeholders with unique markers."""
        if not isinstance(text, str):
            return text, {}
            
        markers = {}
        
        # Protect placeholders first
        def placeholder_replacer(match):
            marker = f"PH{len(markers)}X" # Shorter marker to avoid translator confusion
            markers[marker] = match.group(0)
            return marker
        
        protected_text = PLACEHOLDER_REGEX.sub(placeholder_replacer, text)
        
        # Use provided whitelist or default
        active_whitelist = whitelist if whitelist is not None else DEFAULT_WHITELIST
        
        # Protect whitelisted words (case insensitive for matching, but preserve original)
        for word in sorted(active_whitelist, key=len, reverse=True):
            if not word: continue
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
        # Sort markers by length descending to avoid partial replacements
        for marker, original in sorted(markers.items(), key=lambda x: len(x[0]), reverse=True):
            # Translator often adds spaces or changes case of markers
            # We try to be robust
            pattern = re.compile(re.escape(marker), re.IGNORECASE)
            restored_text = pattern.sub(original, restored_text)
            
            # Also handle cases where translator added spaces around it
            restored_text = restored_text.replace(f" {original} ", f" {original} ")
            
        return restored_text

class TranslationService:
    def __init__(self, source_lang='en', target_lang='en', whitelist=None):
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.whitelist = whitelist
        self.translator = None
        if source_lang != target_lang:
            self.translator = GoogleTranslator(source=source_lang, target=target_lang)

    def translate(self, text, delay=0.2):
        if not self.translator or not isinstance(text, str) or not text.strip() or len(text) < 2:
            return text

        protected_text, markers = TextProtector.protect(text, self.whitelist)
        
        try:
            translated = self.translator.translate(protected_text)
            if delay > 0:
                time.sleep(delay)
            return TextProtector.restore(translated, markers)
        except Exception:
            return text

    def translate_batch(self, texts, delay=0.5):
        """Translates a list of strings efficiently."""
        if not self.translator or not texts:
            return texts

        protected_batch = []
        batch_markers = []

        for text in texts:
            # Punctuation Cleanup: If source doesn't have trailing punctuation, 
            # we'll ensure the translation doesn't either later.
            has_trailing_dot = text.strip().endswith('.')
            
            # UI Nudge: For very short strings, we can sometimes improve "commonness"
            # by ensuring they are treated as standalone labels.
            protected_text, markers = TextProtector.protect(text, self.whitelist)
            protected_batch.append(protected_text)
            
            markers['_meta'] = {'has_trailing_dot': has_trailing_dot}
            batch_markers.append(markers)

        try:
            # We use the base translate method if batch is not available or for better error control
            # but deep-translator's translate_batch is generally faster.
            translated_batch = self.translator.translate_batch(protected_batch)
            
            if delay > 0:
                time.sleep(delay)

            results = []
            for translated, markers in zip(translated_batch, batch_markers):
                restored = TextProtector.restore(translated, markers)
                
                # Post-processing for "shorter/cleaner" results
                if restored and not markers['_meta']['has_trailing_dot']:
                    restored = restored.rstrip('.')
                
                results.append(restored)
            return results
        except Exception as e:
            # Check for rate limit indicators in the error message
            if "429" in str(e) or "Too Many Requests" in str(e):
                raise Exception("RATE_LIMIT_HIT")
            print(f"Batch translation error: {e}")
            return None # Signal failure to the caller for retry

def get_translator_code(locale):
    lang_code = locale.split('-')[0]
    if lang_code in LANG_MAP:
        return LANG_MAP[lang_code](locale)
    return lang_code
