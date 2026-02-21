import time
import re
from deep_translator import GoogleTranslator
from .config import PLACEHOLDER_REGEX, LANG_MAP, WHITELIST as DEFAULT_WHITELIST

class TextProtector:
    @staticmethod
    def protect(text, whitelist=None):
        """Replaces whitelisted words and placeholders with unique markers."""
        markers = {}
        
        # Protect placeholders first
        def placeholder_replacer(match):
            marker = f"__PH_{len(markers)}__"
            markers[marker] = match.group(0)
            return marker
        
        protected_text = PLACEHOLDER_REGEX.sub(placeholder_replacer, text)
        
        # Use provided whitelist or default
        active_whitelist = whitelist if whitelist is not None else DEFAULT_WHITELIST
        
        # Protect whitelisted words (case insensitive for matching, but preserve original)
        for word in sorted(active_whitelist, key=len, reverse=True):
            pattern = re.compile(re.escape(word), re.IGNORECASE)
            
            def word_replacer(match):
                marker = f"__WL_{len(markers)}__"
                markers[marker] = match.group(0)
                return marker
                
            protected_text = pattern.sub(word_replacer, protected_text)
            
        return protected_text, markers

    @staticmethod
    def restore(text, markers):
        """Restores protected markers back to their original text."""
        restored_text = text
        for marker, original in sorted(markers.items(), key=lambda x: len(x[0]), reverse=True):
            restored_text = restored_text.replace(marker, original)
            # Handle cases where translator might have added spaces around markers
            restored_text = restored_text.replace(f" {marker} ", f" {original} ")
            restored_text = restored_text.replace(f" {marker}", f" {original}")
            restored_text = restored_text.replace(f"{marker} ", f"{original} ")
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
            # Fallback to original text on error
            return text

def get_translator_code(locale):
    lang_code = locale.split('-')[0]
    if lang_code in LANG_MAP:
        return LANG_MAP[lang_code](locale)
    return lang_code
