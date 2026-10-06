import pytest

from strif.string_replace import Insertion, Replacement, insert_multiple, replace_multiple


def test_named_tuple_fields_and_positional_compat():
    # NamedTuple gives named access while staying tuple-compatible, so both the named
    # and positional/unpacking APIs must work.
    ins = Insertion(offset=5, text=",")
    assert ins.offset == 5 and ins.text == ","
    assert ins == (5, ",")

    start, end, text = Replacement(0, 3, "x")
    assert (start, end, text) == (0, 3, "x")


def test_insert_multiple():
    assert insert_multiple("hello world", [Insertion(5, ",")]) == "hello, world"
    assert (
        insert_multiple("hello world", [Insertion(0, "Start "), Insertion(11, " End")])
        == "Start hello world End"
    )
    # Out-of-bounds offset clamps to the end.
    assert insert_multiple("short", [Insertion(10, " end")]) == "short end"
    # Negative offset indexes from the end.
    assert insert_multiple("negative test", [Insertion(-1, "ss")]) == "negative tessst"
    assert insert_multiple("no change", []) == "no change"


def test_replace_multiple():
    assert (
        replace_multiple(
            "The quick brown fox", [Replacement(4, 9, "slow"), Replacement(16, 19, "dog")]
        )
        == "The slow brown dog"
    )
    # Out-of-bounds end clamps to the end.
    assert (
        replace_multiple("short text", [Replacement(5, 10, " longer text")]) == "short longer text"
    )
    assert replace_multiple("no change", []) == "no change"


def test_replace_multiple_rejects_overlap():
    with pytest.raises(ValueError):
        replace_multiple("overlap test", [Replacement(0, 6, "start"), Replacement(5, 10, "end")])


#
# ---- Sed-style substitution rules ----

from strif.string_replace import Rule, RuleSet, RuleSyntaxError


def test_rule_parse_basic():
    rule = Rule.parse("s/foo/bar/")
    assert rule.pattern == "foo" and rule.replacement == "bar"
    assert rule.count == 1 and not rule.ignore_case
    assert rule.apply("foo foo") == "bar foo"


def test_rule_parse_arbitrary_delimiters():
    for text in ["s|a/b|c|", "s#a/b#c#", "s:a/b:c:"]:
        rule = Rule.parse(text)
        assert rule.pattern == "a/b" and rule.replacement == "c"
        assert rule.apply("xa/bx") == "xcx"


def test_rule_parse_escaped_delimiter():
    rule = Rule.parse(r"s/a\/b/c\/d/")
    assert rule.pattern == "a/b" and rule.replacement == "c/d"
    assert rule.apply("a/b") == "c/d"
    # Other backslash sequences are preserved for the regex engine.
    rule = Rule.parse(r"s/(\d+)/[\1]/g")
    assert rule.apply("a1b22") == "a[1]b[22]"


def test_rule_flags_compose():
    text = "foo Foo FOO"
    assert Rule.parse("s/foo/bar/").apply(text) == "bar Foo FOO"
    assert Rule.parse("s/foo/bar/g").apply(text) == "bar Foo FOO"
    assert Rule.parse("s/foo/bar/I").apply(text) == "bar Foo FOO"
    assert Rule.parse("s/foo/bar/i").apply(text) == "bar Foo FOO"
    assert Rule.parse("s/foo/bar/2").apply(text) == "bar Foo FOO"
    # g + I + digits all compose: g wins over the count, I folds case.
    assert Rule.parse("s/foo/bar/gI").apply(text) == "bar bar bar"
    assert Rule.parse("s/foo/bar/Ig2").apply(text) == "bar bar bar"
    assert Rule.parse("s/foo/bar/I2").apply(text) == "bar bar FOO"


def test_rule_regex_cache_no_cross_contamination():
    # Same pattern + case sensitivity shares one compiled regex, regardless of
    # g/count flags; different case sensitivity gets a distinct regex.
    assert Rule.parse("s/foo/bar/").regex is Rule.parse("s/foo/bar/").regex
    rule_a = Rule.parse("s/foo/bar/g")
    rule_b = Rule.parse("s/foo/bar/3")
    assert rule_a.regex is rule_b.regex
    assert Rule.parse("s/foo/bar/I").regex is not rule_a.regex


def test_rule_parse_errors_report_line_and_snippet():
    bad_lines = [
        "x/a/b/",
        "s",
        "sab",
        "s/a/b",  # missing trailing delimiter
        "sa",  # invalid delimiter (alphanumeric)
        r"s/a\\",  # dangling backslash
        "s/a/b/x",  # unknown flag
        "s//b/",  # empty pattern
        "s/(/b/",  # invalid regex
    ]
    for line in bad_lines:
        with pytest.raises(RuleSyntaxError):
            Rule.parse(line, line_no=7)
    try:
        Rule.parse("s/a/b", line_no=7)
    except RuleSyntaxError as e:
        assert e.line_no == 7
        assert e.line == "s/a/b"
        assert "line 7" in str(e)
        assert "'s/a/b'" in str(e)


def test_ruleset_parse_skips_comments_and_blank_lines():
    ruleset = RuleSet.parse("# comment\n\ns/a/b/\n   \ns/c/d/g\n")
    assert len(ruleset) == 2
    assert ruleset.apply("a c c") == "b d d"


def test_ruleset_parse_is_fail_fast_with_line_number():
    with pytest.raises(RuleSyntaxError) as exc_info:
        RuleSet.parse("s/a/b/\noops\ns/c/d/\n")
    assert exc_info.value.line_no == 2
    assert exc_info.value.line == "oops"


def test_ruleset_applies_rules_in_order():
    # Each rule sees the output of the previous one.
    ruleset = RuleSet.parse("s/a/b/\ns/b/c/\n")
    assert ruleset.apply("a") == "c"


def test_ruleset_apply_with_counts():
    ruleset = RuleSet.parse("s/a/b/g\ns/z/q/\n")
    new_text, counts = ruleset.apply_with_counts("a a a")
    assert new_text == "b b b"
    assert counts == [3, 0]


def test_rule_not_guaranteed_idempotent():
    # Documented behavior: reapplying a rule may change the text further.
    rule = Rule.parse("s/a/aa/g")
    assert rule.apply(rule.apply("a")) == "aaaa"
    # But an idempotent rule is stable.
    rule = Rule.parse("s/foo/bar/g")
    assert rule.apply(rule.apply("foo")) == "bar"
