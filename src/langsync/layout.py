"""Where locale files live on disk.

Two layouts are supported:

    flat     one file per locale in a single directory, named after the locale:
                 messages/en.json  messages/fr.json  messages/pt-BR.json
             configured with "source" + "dir" (the original langsync layout).

    pattern  any path with a {locale} placeholder, optionally with a {ns}
             (or *) namespace placeholder for i18next-style split files:
                 public/locales/{locale}/{ns}.json
                 src/i18n/{locale}.yaml
                 lib/l10n/app_{locale}.arb
             configured with "pattern" + "source_locale".

Both expose the same small interface so the sync loop does not care which one
is in use: the source files (one per namespace), the locales that exist, and
the target path for a (locale, namespace) pair.
"""

import glob
import os
import re

from .fileio import SUPPORTED_EXTENSIONS, locale_from_filename
from .state import STATE_FILENAME

# Matches BCP 47-ish locale codes as used for file names: en, fr-FR, pt_BR,
# zh-Hant, sr-Latn-RS, es-419. Used to decide whether a file name can tell us
# the source language.
LOCALE_CODE_RE = re.compile(r"^[A-Za-z]{2,3}([-_][A-Za-z0-9]{2,8})*$")

_NS_TOKENS = ("{ns}", "*")


def looks_like_locale(name):
    return bool(name) and bool(LOCALE_CODE_RE.match(name))


class SourceUnit:
    """One source file. `ns` is None for single-file layouts; with namespaces
    it is the namespace name, and snapshot keys for it carry an "ns:" prefix
    so two namespaces with the same key never share a hash."""

    __slots__ = ("ns", "path", "data", "style")

    def __init__(self, ns, path, data=None, style=None):
        self.ns = ns
        self.path = path
        self.data = data
        self.style = style

    @property
    def prefix(self):
        return f"{self.ns}:" if self.ns is not None else ""

    @property
    def is_arb(self):
        return self.path.lower().endswith(".arb")


class FlatLayout:
    kind = "flat"

    def __init__(self, source, directory):
        self.source = source
        self.dir = directory
        self.ext = os.path.splitext(source)[1].lower() or ".json"

    @property
    def source_locale(self):
        name = os.path.splitext(os.path.basename(self.source))[0]
        return name if looks_like_locale(name) else None

    def source_units(self):
        return [SourceUnit(None, self.source)]

    def discover_locales(self):
        """Every locale file in `dir` with the source's extension, except the
        source itself and dotfiles (the snapshot lives there too)."""
        source_name = os.path.basename(self.source)
        source_abs = os.path.abspath(self.source)
        found = set()
        for name in os.listdir(self.dir):
            if name == source_name or name == STATE_FILENAME or name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() != self.ext:
                continue
            full = os.path.join(self.dir, name)
            if not os.path.isfile(full) or os.path.abspath(full) == source_abs:
                continue
            locale = locale_from_filename(name)
            if locale:
                found.add(locale)
        return sorted(found)

    def target_path(self, locale, ns=None):
        return os.path.join(self.dir, f"{locale}{self.ext}")

    def locale_exists(self, locale):
        return os.path.isfile(self.target_path(locale))

    def default_state_dir(self):
        return self.dir

    def describe(self):
        return os.path.join(self.dir, f"<locale>{self.ext}")


class PatternLayout:
    kind = "pattern"

    def __init__(self, pattern, source_locale):
        if "{locale}" not in pattern:
            raise ValueError('"pattern" must contain {locale}, e.g. "locales/{locale}/{ns}.json"')
        ns_tokens = [t for t in _NS_TOKENS if t in pattern]
        if len(ns_tokens) > 1 or pattern.count(ns_tokens[0] if ns_tokens else "\0") > 1:
            raise ValueError('"pattern" may contain at most one namespace placeholder ({ns} or *)')
        ext = os.path.splitext(pattern)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS and not ns_tokens:
            raise ValueError(
                f'"pattern" must end in one of {", ".join(SUPPORTED_EXTENSIONS)} (got "{ext or pattern}")'
            )
        self.pattern = pattern
        self.source_locale = source_locale
        self.ns_token = ns_tokens[0] if ns_tokens else None
        self._regex = self._compile(pattern)

    def _compile(self, pattern):
        out = []
        seen_locale = False
        i = 0
        while i < len(pattern):
            if pattern.startswith("{locale}", i):
                out.append("(?P=locale)" if seen_locale else r"(?P<locale>[^/\\]+?)")
                seen_locale = True
                i += len("{locale}")
            elif self.ns_token and pattern.startswith(self.ns_token, i):
                out.append(r"(?P<ns>[^/\\]+?)")
                i += len(self.ns_token)
            else:
                out.append(re.escape(pattern[i]))
                i += 1
        return re.compile("^" + "".join(out) + "$")

    def _glob(self, locale="*", ns="*"):
        path = self.pattern.replace("{locale}", glob.escape(locale) if locale != "*" else "*")
        if self.ns_token:
            path = path.replace(self.ns_token, ns)
        return glob.glob(path)

    def _match(self, path):
        return self._regex.match(path.replace(os.sep, "/") if os.sep != "/" else path)

    def source_units(self):
        units = []
        for path in sorted(self._glob(self.source_locale)):
            m = self._match(path)
            if not m or not os.path.isfile(path):
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            ns = m.groupdict().get("ns") if self.ns_token else None
            units.append(SourceUnit(ns, path))
        return units

    def discover_locales(self):
        found = set()
        for path in self._glob():
            m = self._match(path)
            if not m:
                continue
            locale = m.group("locale")
            if locale != self.source_locale and looks_like_locale(locale):
                found.add(locale)
        return sorted(found)

    def target_path(self, locale, ns=None):
        path = self.pattern.replace("{locale}", locale)
        if self.ns_token and ns is not None:
            path = path.replace(self.ns_token, ns)
        return path

    def locale_exists(self, locale):
        return locale in self.discover_locales()

    def default_state_dir(self):
        """The directory that holds every locale: the part of the pattern
        before the first placeholder (public/locales/{locale}/… → public/locales)."""
        head = self.pattern.split("{locale}", 1)[0]
        if self.ns_token:
            head = head.split(self.ns_token, 1)[0]
        return os.path.dirname(head) or "."

    def describe(self):
        return self.pattern


def detect_project(root="."):
    """Guess a starter config for a project that has none. Returns a dict with
    either {"source", "dir"} or {"pattern", "source_locale"}, or None.

    Looks in the usual i18n folders for an English source: a flat file
    (messages/en.json) or a per-locale folder (public/locales/en/common.json)."""
    candidates = [
        "messages", "locales", "public/locales", "src/locales", "src/i18n", "i18n",
        "lang", "translations", "assets/i18n", "src/assets/i18n", "l10n", "lib/l10n",
        "resources/lang", "static/locales",
    ]
    english = ("en", "en-US", "en-GB", "en_US", "en_GB")
    for rel in candidates:
        base = os.path.join(root, rel)
        if not os.path.isdir(base):
            continue
        for code in english:
            for ext in SUPPORTED_EXTENSIONS:
                flat = os.path.join(base, code + ext)
                if os.path.isfile(flat):
                    return {"source": os.path.relpath(flat, root), "dir": os.path.relpath(base, root)}
            sub = os.path.join(base, code)
            if os.path.isdir(sub):
                exts = sorted({os.path.splitext(f)[1].lower() for f in os.listdir(sub)} & set(SUPPORTED_EXTENSIONS))
                if exts:
                    return {
                        "pattern": os.path.relpath(os.path.join(base, "{locale}", "{ns}" + exts[0]), root),
                        "source_locale": code,
                    }
        # Flutter ARB convention: app_en.arb
        for name in sorted(os.listdir(base)):
            m = re.match(r"^(.*_)(en(?:[-_][A-Za-z]{2})?)\.arb$", name)
            if m:
                return {
                    "pattern": os.path.relpath(os.path.join(base, m.group(1) + "{locale}.arb"), root),
                    "source_locale": m.group(2),
                }
    return None
