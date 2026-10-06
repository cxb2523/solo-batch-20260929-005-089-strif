from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import NamedTuple

__all__ = [
    "Insertion",
    "insert_multiple",
    "Replacement",
    "replace_multiple",
    "RuleSyntaxError",
    "Rule",
    "RuleSet",
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
# ---- Sed-style substitution rules ----

_REGEX_CACHE_SIZE = 512


@lru_cache(maxsize=_REGEX_CACHE_SIZE)
def _compile_regex(pattern: str, ignore_case: bool) -> re.Pattern[str]:
    """
    Compile and cache a regex for a substitution rule.

    The cache key is exactly `(pattern, ignore_case)`. Occurrence limits (the
    `g` and numeric flags) are deliberately NOT part of the key: they are
    applied per rule at substitution time via `re.sub(count=...)`. This way
    flag combinations compose freely and never cross-contaminate: two rules
    sharing a pattern but differing in `g`/count flags safely reuse the same
    compiled regex, while `I`/`i` always yields a distinct compiled regex.
    """
    return re.compile(pattern, re.IGNORECASE if ignore_case else 0)


class RuleSyntaxError(ValueError):
    """
    Raised when a substitution rule cannot be parsed. Carries the 1-based line
    number (`line_no`) and the original text of the offending line (`line`).
    """

    def __init__(self, message: str, line_no: int | None = None, line: str | None = None):
        self.message: str = message
        self.line_no: int | None = line_no
        self.line: str | None = line
        location = f"line {line_no}" if line_no is not None else "rule"
        snippet = f": {line!r}" if line is not None else ""
        super().__init__(f"Invalid substitution rule at {location}: {message}{snippet}")


@dataclass(frozen=True)
class Rule:
    """
    A single parsed sed-style substitution rule: `s/pattern/replacement/flags`.

    The delimiter may be any punctuation character (not alphanumeric, whitespace,
    or backslash), e.g. `s/a/b/`, `s|a|b|`, `s#a#b#` are all equivalent. Inside
    the pattern and replacement, an escaped delimiter (`\\/`, `\\|`, ...) yields
    a literal delimiter character; all other backslash sequences are preserved
    verbatim so regex escapes (`\\d`) and backreferences (`\\1`) keep working.

    Supported flags, which may be combined in any order:
      g          replace all occurrences (default: first occurrence only)
      I or i     case-insensitive matching
      <digits>   replace at most this many occurrences, e.g. `s/a/b/3`

    If `g` and a numeric count are both given, `g` wins and all occurrences
    are replaced.

    Idempotency: rules are NOT guaranteed to be idempotent. Applying the same
    rule twice may change the text further (e.g. `s/a/aa/g` grows the text on
    every run). If you need idempotence, write rules whose replacement can
    never re-match the pattern.
    """

    pattern: str
    replacement: str
    count: int = 1  # 0 means replace all occurrences.
    ignore_case: bool = False
    line_no: int | None = None
    source: str | None = None

    @classmethod
    def parse(cls, line: str, line_no: int | None = None) -> Rule:
        """
        Parse a single rule line. Raises `RuleSyntaxError` (with `line_no` and
        the original line) on any malformed input; never returns a partial rule.
        """

        def fail(message: str) -> RuleSyntaxError:
            return RuleSyntaxError(message, line_no=line_no, line=line)

        text = line.strip()
        if not text:
            raise fail("empty rule")
        if not text.startswith("s"):
            raise fail("rule must start with 's' (s/pattern/replacement/flags)")
        if len(text) < 2:
            raise fail("missing delimiter after 's'")
        delimiter = text[1]
        if delimiter.isalnum() or delimiter.isspace() or delimiter == "\\":
            raise fail(f"invalid delimiter {delimiter!r}: must be a punctuation character")

        fields: list[str] = []
        buf: list[str] = []
        i = 2
        while i < len(text) and len(fields) < 2:
            ch = text[i]
            if ch == "\\":
                if i + 1 >= len(text):
                    raise fail("dangling backslash at end of rule")
                nxt = text[i + 1]
                if nxt == delimiter:
                    buf.append(delimiter)  # Escaped delimiter becomes a literal.
                else:
                    # Keep other escape sequences (e.g. \d, \1) intact for re.
                    buf.append(ch)
                    buf.append(nxt)
                i += 2
            elif ch == delimiter:
                fields.append("".join(buf))
                buf = []
                i += 1
            else:
                buf.append(ch)
                i += 1
        if len(fields) < 2:
            raise fail(f"expected s{delimiter}pattern{delimiter}replacement{delimiter}flags")

        pattern, replacement = fields
        flags = text[i:].strip()

        count = 1
        ignore_case = False
        number = ""
        saw_global = False
        for ch in flags:
            if ch == "g":
                saw_global = True
            elif ch in "iI":
                ignore_case = True
            elif ch.isdigit():
                number += ch
            else:
                raise fail(f"unknown flag {ch!r} (supported: g, i, I, or a number)")
        if saw_global:
            count = 0
        elif number:
            count = int(number)

        if not pattern:
            raise fail("empty pattern")
        try:
            _compile_regex(pattern, ignore_case)
        except re.error as e:
            raise fail(f"invalid regex: {e}") from e

        return cls(
            pattern=pattern,
            replacement=replacement,
            count=count,
            ignore_case=ignore_case,
            line_no=line_no,
            source=line,
        )

    @property
    def regex(self) -> re.Pattern[str]:
        """
        The compiled regex for this rule, shared via the module-level cache.
        """
        return _compile_regex(self.pattern, self.ignore_case)

    def apply(self, text: str) -> str:
        """
        Apply this rule to `text`, returning the new text.
        """
        return self.regex.sub(self.replacement, text, count=self.count)

    def apply_with_count(self, text: str) -> tuple[str, int]:
        """
        Apply this rule to `text`, returning (new_text, number_of_replacements).
        """
        return self.regex.subn(self.replacement, text, count=self.count)

    def count_matches(self, text: str) -> int:
        """
        Number of replacements `apply()` would perform on `text`.
        """
        total = sum(1 for _ in self.regex.finditer(text))
        return total if self.count == 0 else min(total, self.count)


@dataclass(frozen=True)
class RuleSet:
    """
    An ordered set of substitution rules, applied sequentially: each rule sees
    the output of the previous one.

    Parse failure policy: parsing is FAIL-FAST. The first invalid rule aborts
    the whole parse by raising `RuleSyntaxError` (carrying the 1-based line
    number and the original line text); invalid rules are never silently
    skipped. Blank lines and lines whose first non-space character is `#` are
    ignored as comments.

    Idempotency: `apply()` is a pure, deterministic function of its input, but
    it is NOT guaranteed to be idempotent: running the same `RuleSet` twice may
    change the text further. See `Rule` for details and how to write
    idempotent rules.
    """

    rules: tuple[Rule, ...]

    @classmethod
    def parse(cls, text: str) -> RuleSet:
        """
        Parse a multi-line string of rules, one `s/pat/repl/flags` rule per
        line. Fail-fast: raises `RuleSyntaxError` on the first invalid line.
        """
        rules: list[Rule] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            rules.append(Rule.parse(line, line_no=line_no))
        return cls(tuple(rules))

    def apply(self, text: str) -> str:
        """
        Apply all rules in order, returning the final text.
        """
        for rule in self.rules:
            text = rule.apply(text)
        return text

    def apply_with_counts(self, text: str) -> tuple[str, list[int]]:
        """
        Apply all rules in order, returning (final_text, per-rule replacement
        counts).
        """
        counts: list[int] = []
        for rule in self.rules:
            text, n = rule.apply_with_count(text)
            counts.append(n)
        return text, counts

    def __len__(self) -> int:
        return len(self.rules)

    def __iter__(self):
        return iter(self.rules)
