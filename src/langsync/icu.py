"""Minimal ICU MessageFormat support: parse, extract translatable spans, rebuild.

langsync must never send ICU structure (argument names, argument types, branch
keywords, braces, '#') to the translation API — only the human-readable text.
This module provides just enough syntax awareness for that:

    parse(text)          -> ParsedMessage (raises ICUParseError on invalid ICU)
    ParsedMessage.translatable_spans()
                         -> the character ranges a translator may touch
    ParsedMessage.rebuild(replacements)
                         -> splice translated spans back into the original,
                            leaving the structural skeleton byte-identical

Because rebuild() only ever replaces the extracted spans, reserved tokens are
structurally incapable of being altered. Callers still re-parse() the rebuilt
message as a final guard against a translation that introduces stray braces.
"""

PLURAL_KEYWORDS = {"zero", "one", "two", "few", "many", "other"}
BRANCHING_TYPES = {"plural", "selectordinal", "select"}

# Characters after which an apostrophe starts an ICU quoted literal.
_QUOTABLE = set("{}#|<")


class ICUParseError(ValueError):
    def __init__(self, message, pos=None):
        suffix = f" (at position {pos})" if pos is not None else ""
        super().__init__(message + suffix)
        self.pos = pos


class ParsedMessage:
    """A parsed message plus the spans of translator-visible text.

    spans is a sorted list of (start, end, in_plural) character ranges covering
    every literal-text run (simple `{name}` and formatted `{n, number}`
    arguments stay inside their run so markers can travel through translation).
    Everything *outside* the spans is structural skeleton. in_plural marks
    spans that live inside a plural/selectordinal branch, where '#' is the
    number placeholder and must be preserved.
    """

    __slots__ = ("text", "spans", "has_branching")

    def __init__(self, text, spans, has_branching):
        self.text = text
        self.spans = spans
        self.has_branching = has_branching

    def translatable_spans(self):
        """Returns [(index, raw_text, in_plural)] for spans worth translating."""
        out = []
        for i, (start, end, in_plural) in enumerate(self.spans):
            raw = self.text[start:end]
            if any(ch.isalpha() for ch in raw):
                out.append((i, raw, in_plural))
        return out

    def rebuild(self, replacements):
        """Splice replacement text into the message. replacements maps a span
        index (as yielded by translatable_spans) to its new text. Spans without
        a replacement — and everything between spans — are kept verbatim."""
        out = []
        last = 0
        for i, (start, end, _) in enumerate(self.spans):
            if i in replacements:
                out.append(self.text[last:start])
                out.append(replacements[i])
                last = end
        out.append(self.text[last:])
        return "".join(out)


class _Parser:
    def __init__(self, text):
        self.text = text
        self.pos = 0
        self.spans = []
        self.has_branching = False

    def _peek(self):
        return self.text[self.pos] if self.pos < len(self.text) else None

    def _skip_ws(self):
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def _read_token(self, extra_stops=""):
        start = self.pos
        stops = "{}," + extra_stops
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch.isspace() or ch in stops:
                break
            self.pos += 1
        return self.text[start:self.pos]

    def _consume_quoted(self):
        """Handle ICU apostrophe escaping. self.pos is at the apostrophe."""
        nxt = self.text[self.pos + 1] if self.pos + 1 < len(self.text) else None
        if nxt == "'":
            self.pos += 2  # '' -> literal apostrophe
            return
        if nxt in _QUOTABLE:
            self.pos += 2  # quoted literal until the closing apostrophe
            while self.pos < len(self.text):
                if self.text[self.pos] == "'":
                    if self.pos + 1 < len(self.text) and self.text[self.pos + 1] == "'":
                        self.pos += 2
                        continue
                    self.pos += 1
                    return
                self.pos += 1
            return  # unterminated quote runs to end of message (ICU is lenient)
        self.pos += 1  # plain apostrophe

    def _end_span(self, start, end, in_plural):
        if end > start:
            self.spans.append((start, end, in_plural))

    def parse_message(self, in_plural, at_top):
        """Parse until '}' (branch body) or end of input (top level), recording
        translatable spans along the way."""
        span_start = self.pos
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch == "}":
                if at_top:
                    raise ICUParseError("unmatched '}'", self.pos)
                break
            if ch == "'":
                self._consume_quoted()
                continue
            if ch == "{":
                arg_start = self.pos
                if self._parse_argument(in_plural):
                    # Branching argument: everything before it closes the
                    # current span; its branch bodies contributed their own.
                    self._end_span(span_start, arg_start, in_plural)
                    span_start = self.pos
                continue
            self.pos += 1
        self._end_span(span_start, self.pos, in_plural)

    def _parse_argument(self, in_plural):
        """Parse an argument starting at '{'. Returns True when the argument is
        plural/selectordinal/select (i.e. it breaks the surrounding span)."""
        brace_pos = self.pos
        self.pos += 1
        self._skip_ws()
        name = self._read_token()
        if not name:
            raise ICUParseError("empty argument name", brace_pos)
        self._skip_ws()
        ch = self._peek()
        if ch == "}":
            self.pos += 1  # simple argument: {name}
            return False
        if ch != ",":
            raise ICUParseError(f"expected ',' or '}}' in argument '{name}'", self.pos)
        self.pos += 1
        self._skip_ws()
        arg_type = self._read_token()
        if not arg_type:
            raise ICUParseError(f"missing type in argument '{name}'", self.pos)
        self._skip_ws()
        if arg_type in BRANCHING_TYPES:
            if self._peek() != ",":
                raise ICUParseError(
                    f"expected ',' after '{arg_type}' in argument '{name}'", self.pos
                )
            self.pos += 1
            self.has_branching = True
            self._parse_branches(name, arg_type, in_plural)
            return True
        # number/date/time/…: optionally a style, then the closing brace.
        if self._peek() == ",":
            self._consume_style(name)
        if self._peek() != "}":
            raise ICUParseError(f"unterminated argument '{name}'", self.pos)
        self.pos += 1
        return False

    def _consume_style(self, name):
        """Consume a formatted-argument style (e.g. '::currency/GBP') up to,
        but not including, the argument's closing brace."""
        self.pos += 1  # the ',' before the style
        depth = 0
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch == "'":
                self._consume_quoted()
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                if depth == 0:
                    return
                depth -= 1
            self.pos += 1
        raise ICUParseError(f"unterminated argument '{name}'", self.pos)

    def _parse_branches(self, name, arg_type, outer_in_plural):
        in_plural = True if arg_type in ("plural", "selectordinal") else outer_in_plural
        selectors = []
        while True:
            self._skip_ws()
            ch = self._peek()
            if ch is None:
                raise ICUParseError(f"unterminated '{arg_type}' argument '{name}'", self.pos)
            if ch == "}":
                self.pos += 1
                break
            sel_pos = self.pos
            selector = self._read_token(extra_stops=":")
            if not selector:
                raise ICUParseError(
                    f"expected a selector in '{arg_type}' argument '{name}'", sel_pos
                )
            if self._peek() == ":" and selector == "offset":
                if arg_type == "select":
                    raise ICUParseError(f"'offset' is not allowed in select argument '{name}'", sel_pos)
                if selectors:
                    raise ICUParseError(f"'offset' must precede the branches of '{name}'", sel_pos)
                self.pos += 1
                self._skip_ws()
                value = self._read_token()
                if not value or not value.lstrip("-").isdigit():
                    raise ICUParseError(f"invalid offset value in argument '{name}'", sel_pos)
                continue
            if arg_type in ("plural", "selectordinal"):
                valid = selector in PLURAL_KEYWORDS or (
                    selector.startswith("=") and selector[1:].isdigit()
                )
                if not valid:
                    raise ICUParseError(
                        f"invalid {arg_type} selector '{selector}' in argument '{name}' "
                        f"(expected one of {sorted(PLURAL_KEYWORDS)} or '=N')",
                        sel_pos,
                    )
            self._skip_ws()
            if self._peek() != "{":
                raise ICUParseError(
                    f"expected '{{' after selector '{selector}' in argument '{name}'", self.pos
                )
            selectors.append(selector)
            self.pos += 1
            self.parse_message(in_plural=in_plural, at_top=False)
            if self._peek() != "}":
                raise ICUParseError(
                    f"unterminated branch '{selector}' in argument '{name}'", self.pos
                )
            self.pos += 1
        if "other" not in selectors:
            raise ICUParseError(
                f"'{arg_type}' argument '{name}' is missing the mandatory 'other' branch"
            )


def parse(text):
    """Parse an ICU MessageFormat string. Raises ICUParseError on invalid input."""
    if not isinstance(text, str):
        raise ICUParseError("not a string")
    parser = _Parser(text)
    parser.parse_message(in_plural=False, at_top=True)
    parser.spans.sort(key=lambda span: span[0])
    return ParsedMessage(text, parser.spans, parser.has_branching)


def is_valid(text):
    """True when text parses cleanly as ICU MessageFormat."""
    try:
        parse(text)
    except ICUParseError:
        return False
    return True
