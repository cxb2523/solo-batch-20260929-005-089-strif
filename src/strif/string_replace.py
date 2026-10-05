"""
Offset-based edit primitives plus a pure-function layer for sed-style
replacement rules. This module does no I/O: file orchestration and atomic
write-back live in `strif.strif`.

Rule syntax (one per line):

    s<delim>pattern<delim>replacement<delim>flags

- Any non-alphanumeric, non-backslash, non-whitespace character may be the
  delimiter, e.g. `s/a/b/g`, `s|a|b|g`, `s#a#b#`.
- The delimiter may be used literally inside the pattern or replacement by
  escaping it with a backslash (e.g. `s/path\\/to/file/PATH/`). Other
  backslash sequences are passed through untouched, so regex escapes (`\\d`,
  `\\b`, ...) and group references (`\\1`) work as in Python `re`.
- In the replacement, an unescaped `&` expands to the whole match (sed
  semantics); `\\&` is a literal ampersand.
- Flags may combine freely, with one exception:
    - `g`    replace all occurrences (default: first occurrence only)
    - `I`    case-insensitive matching
    - `<n>`  a decimal number: replace only the nth occurrence
  `g` and `<n>` are mutually exclusive (a global nth-onward replace was
  considered too surprising); combining them is a parse error, as are
  duplicate flags and unknown flags.
- Blank lines and lines starting with `#` are ignored.

Design decisions (read before changing):

- Parse failures ABORT, never skip: `RuleSet.from_text` raises
  `RuleParseError` on the first invalid rule, reporting the 1-based line
  number and the original line text. A partially applied rule set is
  considered worse than no application at all, especially since callers pair
  this with atomic file write-back.
- Compiled regexes are cached by (pattern, ignore_case) only. The `g` and
  numeric flags are application-time semantics, not regex-compile options,
  so all flag combinations share cached regexes without affecting each
  other's behavior.
- Re-applying the same rule is NOT guaranteed to be idempotent (e.g.
  `s/a/aa/g` grows the text on every run). `apply` is pure -- it never
  mutates the Rule -- so a repeated application simply runs the substitution
  again on the new input. Callers needing idempotence must choose their
  rules accordingly.
- Errors while applying a successfully parsed rule (e.g. a replacement
  referencing a nonexistent group) raise `RuleApplicationError`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

__all__ = [
    "Insertion",
    "insert_multiple",
    "Replacement",
    "replace_multiple",
    "Rule",
    "RuleParseError",
    "RuleApplicationError",
    "RuleResult",
    "RuleSet",
    "RuleSetResult",
    "FileRuleResult",
]


class Insertion(NamedTuple):
    offset: int
    text: str


def insert_multiple(text: str, insertions: list[Insertion]) -> str:
    """
    Insert multiple strings into `text` at the given offsets, at once.
    """
    chunks: list[str] = []
    last_end = 0
    for offset, insertion in sorted(insertions, key=lambda x: x[0]):
        chunks.append(text[last_end:offset])
        chunks.append(insertion)
        last_end = offset
    chunks.append(text[last_end:])
    return "".join(chunks)


class Replacement(NamedTuple):
    start: int
    end: int
    text: str


def replace_multiple(text: str, replacements: list[Replacement]) -> str:
    """
    Replace multiple substrings in `text` with new strings, simultaneously.
    The replacements are a list of tuples (start_offset, end_offset, new_string).
    """
    replacements = sorted(replacements, key=lambda x: x[0])
    chunks: list[str] = []
    last_end = 0
    for start, end, new_text in replacements:
        if start < last_end:
            raise ValueError("Overlapping replacements are not allowed.")
        chunks.append(text[last_end:start])
        chunks.append(new_text)
        last_end = end
    chunks.append(text[last_end:])
    return "".join(chunks)


#
# ---- Sed-style replacement rules ----


class RuleParseError(ValueError):
    """
    A sed rule failed to parse. Carries the 1-based `line_number` of the rule
    within its source text and the original `raw` line itself.
    """

    line_number: int
    raw: str
    reason: str

    def __init__(self, line_number: int, raw: str, reason: str):
        self.line_number = line_number
        self.raw = raw
        self.reason = reason
        super().__init__(f"Invalid sed rule at line {line_number}: {raw!r} ({reason})")


class RuleApplicationError(ValueError):
    """
    A rule parsed successfully but failed while being applied, e.g. a
    replacement referencing a regex group that does not exist.
    """


@lru_cache(maxsize=512)
def _compile_regex(pattern_str: str, ignore_case: bool) -> re.Pattern[str]:
    """
    Compile and cache a regex. The cache key is (pattern_str, ignore_case)
    only: `g` and numeric occurrence flags are apply-time semantics, not
    compile options, so every flag combination reuses the same compiled
    pattern without the combinations influencing one another.
    """
    return re.compile(pattern_str, re.IGNORECASE if ignore_case else 0)


_SNIPPET_RADIUS = 40


def _snippet(text: str, start: int, end: int) -> str:
    """A window of text around [start, end), for before/after previews."""
    return text[max(0, start - _SNIPPET_RADIUS) : end + _SNIPPET_RADIUS]


class RuleResult(str):
    """
    The result of applying one rule: behaves as the new text (a `str`), with
    `hits`, `before_snippet`, and `after_snippet` metadata attached.
    """

    hits: int
    before_snippet: str
    after_snippet: str

    def __new__(
        cls, new_text: str, hits: int = 0, before_snippet: str = "", after_snippet: str = ""
    ):
        obj = super().__new__(cls, new_text)
        obj.hits = hits
        obj.before_snippet = before_snippet
        obj.after_snippet = after_snippet
        return obj

    @property
    def new_text(self) -> str:
        return str(self)


def _translate_replacement(replacement: str) -> str:
    """
    Translate sed replacement syntax to a Python `re` template: an unescaped
    `&` becomes `\\g<0>` (the whole match); `\\&` becomes a literal `&`.
    Group references (`\\1`, `\\g<name>`) pass through untouched.
    """
    out: list[str] = []
    i = 0
    while i < len(replacement):
        ch = replacement[i]
        if ch == "\\" and i + 1 < len(replacement):
            out.append("&" if replacement[i + 1] == "&" else replacement[i : i + 2])
            i += 2
        elif ch == "&":
            out.append("\\g<0>")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _split_rule_body(body: str, delim: str) -> tuple[str, str, str]:
    """
    Split `pattern<delim>replacement<delim>flags` on unescaped delimiters.
    Backslash-escaped delimiters (`\\/`) are unescaped in the results; all
    other backslash sequences are preserved for the regex engine.
    """
    segments: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(body) and len(segments) < 2:
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            current.append(body[i : i + 2])
            i += 2
        elif ch == delim:
            segments.append("".join(current))
            current = []
            i += 1
        else:
            current.append(ch)
            i += 1
    if len(segments) < 2:
        raise ValueError("missing delimiter")
    pattern, replacement = (s.replace("\\" + delim, delim) for s in segments)
    return pattern, replacement, body[i:].strip()


@dataclass(frozen=True)
class Rule:
    """A single parsed sed-style substitution rule."""

    pattern_str: str
    replacement: str
    global_flag: bool = False
    ignore_case: bool = False
    occurrence: int | None = None
    delimiter: str = "/"
    raw: str = ""
    line_number: int = 0

    @classmethod
    def parse(cls, line: str, line_number: int = 0) -> Rule:
        """
        Parse one sed-style substitution rule. Raises `RuleParseError` --
        with the line number and the original line text -- if the line is
        not a valid rule.
        """
        raw = line.rstrip("\r\n")
        stripped = raw.strip()

        def fail(reason: str) -> RuleParseError:
            return RuleParseError(line_number, stripped, reason)

        if len(stripped) < 2 or not stripped.startswith("s"):
            raise fail("rule must have the form s<delim>pattern<delim>replacement<delim>flags")
        delim = stripped[1]
        if delim.isalnum() or delim == "\\" or delim.isspace():
            raise fail(f"invalid delimiter {delim!r}")
        try:
            pattern_str, replacement, flags = _split_rule_body(stripped[2:], delim)
        except ValueError:
            raise fail(f"expected s{delim}pattern{delim}replacement{delim}flags") from None
        if not pattern_str:
            raise fail("empty pattern")

        global_flag = False
        ignore_case = False
        occurrence: int | None = None
        digits: list[str] = []
        for ch in flags:
            if ch == "g":
                if global_flag:
                    raise fail("duplicate 'g' flag")
                global_flag = True
            elif ch == "I":
                if ignore_case:
                    raise fail("duplicate 'I' flag")
                ignore_case = True
            elif ch.isdigit():
                digits.append(ch)
            else:
                raise fail(f"unknown flag {ch!r} (supported: g, I, <number>)")
        if digits:
            occurrence = int("".join(digits))
            if occurrence < 1:
                raise fail("occurrence number must be >= 1")
            if global_flag:
                raise fail("flags 'g' and <number> cannot be combined")

        try:
            _compile_regex(pattern_str, ignore_case)
        except re.error as e:
            raise fail(f"bad regex: {e}") from None

        return cls(
            pattern_str=pattern_str,
            replacement=replacement,
            global_flag=global_flag,
            ignore_case=ignore_case,
            occurrence=occurrence,
            delimiter=delim,
            raw=stripped,
            line_number=line_number,
        )

    @property
    def pattern(self) -> re.Pattern[str]:
        """The compiled regex, shared via the (pattern_str, ignore_case) cache."""
        return _compile_regex(self.pattern_str, self.ignore_case)

    @property
    def flags_str(self) -> str:
        parts: list[str] = []
        if self.global_flag:
            parts.append("g")
        if self.ignore_case:
            parts.append("I")
        if self.occurrence is not None:
            parts.append(str(self.occurrence))
        return "".join(parts)

    def apply(self, text: str) -> RuleResult:
        """
        Apply this rule to `text`. Returns a `RuleResult` (a `str` of the new
        text) with `hits` and before/after snippets around the first match.

        - no flags: replace the first occurrence
        - `g`:      replace all occurrences
        - `<n>`:    replace only the nth occurrence (0 hits if fewer than n)

        Raises `RuleApplicationError` if the replacement template is invalid
        for the match (e.g. references a nonexistent group).
        """
        regex = self.pattern
        replacement = _translate_replacement(self.replacement)
        try:
            if self.occurrence is not None:
                matches = list(regex.finditer(text))
                idx = self.occurrence - 1
                if len(matches) <= idx:
                    return RuleResult(text, 0, text[:_SNIPPET_RADIUS], text[:_SNIPPET_RADIUS])
                m = matches[idx]
                expanded = m.expand(replacement)
                new_text = text[: m.start()] + expanded + text[m.end() :]
                return RuleResult(
                    new_text,
                    1,
                    _snippet(text, m.start(), m.end()),
                    _snippet(new_text, m.start(), m.start() + len(expanded)),
                )

            new_text, hits = regex.subn(replacement, text, count=0 if self.global_flag else 1)
            first = regex.search(text) if hits else None
            if first:
                expanded = first.expand(replacement)
                return RuleResult(
                    new_text,
                    hits,
                    _snippet(text, first.start(), first.end()),
                    _snippet(new_text, first.start(), first.start() + len(expanded)),
                )
            return RuleResult(new_text, hits, text[:_SNIPPET_RADIUS], text[:_SNIPPET_RADIUS])
        except re.error as e:
            raise RuleApplicationError(
                f"Failed to apply rule at line {self.line_number}: {self.raw!r} ({e})"
            ) from e


@dataclass(frozen=True)
class RuleSetResult:
    """The result of applying a whole RuleSet: final text plus per-rule results."""

    final_text: str
    results: tuple[RuleResult, ...] = field(default_factory=tuple)

    @property
    def total_hits(self) -> int:
        return sum(result.hits for result in self.results)


@dataclass(frozen=True)
class RuleSet:
    """
    An ordered set of sed-style rules, applied sequentially: each rule sees
    the output of the previous one. See the module docstring for the rule
    syntax and the parse-failure, caching, and idempotency policies.
    """

    rules: tuple[Rule, ...] = field(default_factory=tuple)

    @classmethod
    def from_text(cls, text: str) -> RuleSet:
        """
        Parse a multi-line rules text. Blank lines and `#` comments are
        skipped (line numbers are preserved). The first invalid rule aborts
        the whole parse with `RuleParseError`; nothing is applied.
        """
        rules: list[Rule] = []
        for line_number, raw in enumerate(text.splitlines(), start=1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            rules.append(Rule.parse(raw, line_number))
        return cls(tuple(rules))

    def apply(self, text: str) -> RuleSetResult:
        """Apply all rules in order, returning the final text and per-rule results."""
        results: list[RuleResult] = []
        current = text
        for rule in self.rules:
            result = rule.apply(current)
            results.append(result)
            current = result.new_text
        return RuleSetResult(current, tuple(results))


@dataclass(frozen=True)
class FileRuleResult:
    """
    The outcome of applying a rule set to a whole file: whether a write
    happened, the final text, and the per-rule results. This is a pure data
    type; the file orchestration itself lives in `strif.strif`.
    """

    path: Path
    wrote: bool
    final_text: str
    results: tuple[RuleResult, ...] = field(default_factory=tuple)
