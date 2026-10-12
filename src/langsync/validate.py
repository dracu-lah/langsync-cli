"""Post-translation checks: does a translated string still carry the source's
placeholders and markup?

    extract_placeholders(text) -> Counter of variable/tag tokens
    check_translation(source, target) -> list of human-readable problems

The checks are deliberately conservative: placeholders may be reordered (word
order differs between languages), spacing inside braces may change, and ICU
plural branches may be added or dropped (Japanese has only 'other', Polish
adds 'few'/'many'), so none of that is reported.
"""

import re
from collections import Counter

from . import icu
from .config import PLACEHOLDER_REGEX

# Protected tokens that are content rather than variables: a translator may
# legitimately localise a URL or an email address, or write "&" for "&amp;".
_CONTENT_RE = re.compile(
    r'^(?:https?://|&(?:[A-Za-z][A-Za-z0-9]*|#\d+|#[xX][0-9A-Fa-f]+);$|[^@\s]+@[^@\s]+$)'
)
_TAG_RE = re.compile(r'^<(/?)\s*([A-Za-z][\w:.-]*|\d+)')
# A branching ICU argument's opening, "{count, plural,", in the skeleton.
_ICU_ARG_RE = re.compile(r'\{\s*([^\s{},]+)\s*,')


def _normalise(token):
    """Canonical form for comparison. Tags keep only name and direction
    (<a href="x"> == <a>, <br/> == <br>); brace tokens ignore inner spacing."""
    m = _TAG_RE.match(token)
    if m:
        return f"<{m.group(1)}{m.group(2).lower()}>"
    if token.startswith(('{', '%{', '${', '$t(')):
        return re.sub(r'\s+', '', token)
    return token


def _icu_message(text):
    """The parsed message when text is valid ICU with plural/select, else None."""
    if not isinstance(text, str) or "{" not in text:
        return None
    try:
        parsed = icu.parse(text)
    except icu.ICUParseError:
        return None
    return parsed if parsed.has_branching else None


def extract_placeholders(text):
    """Counter of the variable and tag tokens in text (URLs, emails and HTML
    entities excluded). For ICU plural/select messages the tokens are the
    argument names as "{name}" plus the tags, each counted once, since branch
    bodies legitimately repeat or drop them."""
    if not isinstance(text, str):
        return Counter()
    parsed = _icu_message(text)
    if parsed is not None:
        # Literal-text spans carry the simple arguments and tags; whatever is
        # left (the skeleton) names the plural/select arguments.
        found = set()
        skeleton = []
        last = 0
        for start, end, _in_plural in parsed.spans:
            skeleton.append(text[last:start])
            last = end
            for tok in PLACEHOLDER_REGEX.findall(text[start:end]):
                if not _CONTENT_RE.match(tok):
                    found.add(_normalise(tok))
        skeleton.append(text[last:])
        for m in _ICU_ARG_RE.finditer(" ".join(skeleton)):
            found.add("{" + m.group(1) + "}")
        return Counter(found)
    tokens = Counter()
    for tok in PLACEHOLDER_REGEX.findall(text):
        if _CONTENT_RE.match(tok):
            continue
        tokens[_normalise(tok)] += 1
    return tokens


def check_translation(source, target):
    """Compare a translation with its source. Returns a list of problems such
    as "missing placeholder {name}", "unexpected placeholder {nmae}",
    "tag mismatch <b>", "empty translation" or "invalid ICU"; [] when fine or
    when either side is not a string."""
    if not isinstance(source, str) or not isinstance(target, str):
        return []
    if not target.strip():
        return ["empty translation"] if source.strip() else []

    if "{" in source and icu.is_valid(source) and not icu.is_valid(target):
        # The placeholder comparison would only add noise on a broken message.
        return ["invalid ICU"]

    problems = []
    src = extract_placeholders(source)
    tgt = extract_placeholders(target)
    tag_issues = set()
    for token in sorted(set(src) | set(tgt)):
        is_tag = token.startswith("<")
        if src[token] > tgt[token]:
            if is_tag:
                tag_issues.add(token)
            else:
                problems.append(f"missing placeholder {token}")
        elif tgt[token] > src[token]:
            if is_tag:
                tag_issues.add(token)
            elif src[token] == 0:
                problems.append(f"unexpected placeholder {token}")
            else:
                problems.append(f"duplicated placeholder {token}")
    for token in sorted(tag_issues):
        problems.append(f"tag mismatch {token}")
    return problems
